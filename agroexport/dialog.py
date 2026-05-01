# dialog.py
# Diálogo principal do plugin AgroExport.

import os

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QLabel, QComboBox, QDoubleSpinBox, QSpinBox, QPushButton, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox,
    QLineEdit, QGroupBox, QProgressBar, QMessageBox, QSizePolicy,
    QFormLayout
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QVariant
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsField,
    QgsWkbTypes, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsPointXY
)
from qgis.utils import iface

from .exporter import (
    collect_lines, simplify_layer, count_verts,
    export_jd_zip, export_aggps_zip, export_agdata_zip,
    export_gs3_zip, ascii_safe
)

TIPOS = ["Curva", "AB", "Limite"]


# ── Worker thread ──────────────────────────────────────────────

class Worker(QThread):
    progress = pyqtSignal(int)
    done     = pyqtSignal(object, dict)

    def __init__(self, layer, tol, min_verts, max_verts):
        super().__init__()
        self.layer     = layer
        self.tol       = tol
        self.min_verts = min_verts
        self.max_verts = max_verts

    def run(self):
        result, stats = simplify_layer(
            self.layer, self.tol, self.progress.emit,
            self.min_verts, self.max_verts
        )
        self.done.emit(result, stats)


# ── Diálogo principal ─────────────────────────────────────────

class AgroDialog(QDialog):
    def __init__(self):
        super().__init__(iface.mainWindow())
        self.setWindowTitle("AgroExport")
        self.setMinimumSize(580, 460)
        self.resize(700, 580)
        self.simplified = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._tab_simpl(),  "1 · Simplificação")
        tabs.addTab(self._tab_class(),  "2 · Classificação")
        tabs.addTab(self._tab_export(), "3 · Exportação")
        root.addWidget(tabs)
        self.status = QLabel("Pronto.")
        self.status.setStyleSheet("color:gray;font-size:11px")
        root.addWidget(self.status)

    # ── Aba 1: Simplificação ────────────────────────────────────
    def _tab_simpl(self):
        w = QWidget()
        L = QVBoxLayout(w)

        g1 = QGroupBox("Camada de entrada")
        r1 = QHBoxLayout(g1)
        r1.addWidget(QLabel("Camada:"))
        self.cb = QComboBox()
        self.cb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for lid, lyr in QgsProject.instance().mapLayers().items():
            if isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == QgsWkbTypes.LineGeometry:
                self.cb.addItem(lyr.name(), lid)
        r1.addWidget(self.cb)
        L.addWidget(g1)

        g2 = QGroupBox("Douglas-Peucker (redução de vértices)")
        r2 = QHBoxLayout(g2)
        r2.addWidget(QLabel("Tolerância:"))
        self.tol = QDoubleSpinBox()
        self.tol.setRange(0.01, 100)
        self.tol.setValue(1.0)
        self.tol.setDecimals(2)
        self.tol.setSuffix(" m")
        r2.addWidget(self.tol)
        r2.addStretch()
        self.lbl_prev = QLabel("Vértices: —")
        r2.addWidget(self.lbl_prev)
        btn_prev = QPushButton("Preview")
        btn_prev.clicked.connect(self._preview)
        r2.addWidget(btn_prev)
        L.addWidget(g2)

        g3 = QGroupBox("Intervalo de vértices")
        r3 = QHBoxLayout(g3)
        r3.addWidget(QLabel("Mínimo:"))
        self.sp_min = QSpinBox()
        self.sp_min.setRange(2, 100)
        self.sp_min.setValue(5)
        self.sp_min.setToolTip("Linhas com menos vértices após simplificação revertem para a geometria original")
        r3.addWidget(self.sp_min)
        r3.addSpacing(16)
        r3.addWidget(QLabel("Máximo:"))
        self.sp_max = QSpinBox()
        self.sp_max.setRange(2, 500)
        self.sp_max.setValue(15)
        self.sp_max.setToolTip("Linhas com mais vértices terão tolerância aumentada iterativamente até atingir o limite; as que não conseguirem são sinalizadas com ⚠")
        r3.addWidget(self.sp_max)
        r3.addStretch()
        L.addWidget(g3)

        self.pb = QProgressBar()
        self.pb.setValue(0)
        L.addWidget(self.pb)

        btn_run = QPushButton("Simplificar")
        btn_run.setStyleSheet("font-weight:bold;padding:6px")
        btn_run.clicked.connect(self._run_simpl)
        L.addWidget(btn_run)

        self.lbl_res = QLabel("")
        self.lbl_res.setWordWrap(True)
        L.addWidget(self.lbl_res)
        L.addStretch()
        return w

    # ── Aba 2: Classificação ────────────────────────────────────
    def _tab_class(self):
        w = QWidget()
        L = QVBoxLayout(w)
        L.addWidget(QLabel("Preencha os atributos. Duplo clique para editar."))
        btn_load = QPushButton("Carregar feições")
        btn_load.clicked.connect(self._load_table)
        L.addWidget(btn_load)

        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["ID", "Cliente", "Fazenda", "Talhão", "Tipo"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tbl.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.SelectedClicked
        )
        L.addWidget(self.tbl)

        # Preenchimento em lote
        g_batch = QGroupBox("Preenchimento em lote")
        fb = QFormLayout(g_batch)
        self.le_cliente = QLineEdit()
        self.le_fazenda = QLineEdit()
        self.le_talhao  = QLineEdit()
        fb.addRow("Cliente:", self.le_cliente)
        fb.addRow("Fazenda:", self.le_fazenda)
        fb.addRow("Talhão:",  self.le_talhao)
        btn_batch = QPushButton("Aplicar a todas as linhas")
        btn_batch.clicked.connect(self._batch_fill)
        fb.addRow(btn_batch)
        L.addWidget(g_batch)

        btn_apply = QPushButton("Salvar")
        btn_apply.setStyleSheet("font-weight:bold;padding:6px")
        btn_apply.clicked.connect(self._apply)
        L.addWidget(btn_apply)
        return w

    # ── Aba 3: Exportação ───────────────────────────────────────
    def _tab_export(self):
        w = QWidget()
        L = QVBoxLayout(w)
        L.setSpacing(8)

        g1 = QGroupBox("Formatos de saída")
        gf = QVBoxLayout(g1)
        self.chk_jd = QCheckBox("John Deere  (GEN4 + GS3_2630)")
        self.chk_jd.setChecked(True)
        self.chk_jd.setToolTip(
            "GEN4: MasterData.xml + AdaptiveCurve .gjson → operationscenter.deere.com\n"
            "GS3_2630: SpatialCatalog + setup.fds → cartão SD › GS3_2630/"
        )
        self.chk_ptx = QCheckBox("PTX Trimble  (AgData + AgGPS)")
        self.chk_ptx.setChecked(True)
        self.chk_ptx.setToolTip(
            "AgData: AgData/Fields/{uuid}.agf → USB › monitor › Importar\n"
            "AgGPS: Shapefile WGS84 → cartão SD › AgGPS/Data/"
        )
        gf.addWidget(self.chk_jd)
        gf.addWidget(self.chk_ptx)
        L.addWidget(g1)

        g2 = QGroupBox("Pasta de saída")
        go = QHBoxLayout(g2)
        self.le_out = QLineEdit()
        self.le_out.setPlaceholderText("Selecione a pasta…")
        go.addWidget(self.le_out)
        btn_br = QPushButton("Procurar…")
        btn_br.clicked.connect(
            lambda: self.le_out.setText(
                QFileDialog.getExistingDirectory(self, "Pasta de saída", "")
            )
        )
        go.addWidget(btn_br)
        L.addWidget(g2)

        btn_exp = QPushButton("Exportar")
        btn_exp.setStyleSheet("font-weight:bold;padding:8px;font-size:13px")
        btn_exp.clicked.connect(self._export)
        L.addWidget(btn_exp)

        self.lbl_exp = QLabel("")
        self.lbl_exp.setWordWrap(True)
        L.addWidget(self.lbl_exp)
        L.addStretch()
        return w

    # ── Lógica aba 1 ──────────────────────────────────────────
    def _cur_layer(self):
        lid = self.cb.currentData()
        return QgsProject.instance().mapLayer(lid) if lid else None

    def _preview(self):
        lyr = self._cur_layer()
        if not lyr:
            return
        tol = self.tol.value()
        vb = va = 0
        for feat in lyr.getFeatures():
            g = feat.geometry()
            if not g or g.isEmpty():
                continue
            vb += count_verts(g)
            sg = g.simplify(tol)
            va += count_verts(sg) if not sg.isEmpty() else count_verts(g)
        pct = (1 - va / vb) * 100 if vb else 0
        self.lbl_prev.setText(f"{vb:,} → {va:,}  (-{pct:.1f}%)")

    def _run_simpl(self):
        lyr = self._cur_layer()
        if not lyr:
            QMessageBox.warning(self, "AgroExport", "Selecione uma camada.")
            return
        self.pb.setValue(0)
        self.lbl_res.setText("Processando…")
        self._worker = Worker(lyr, self.tol.value(), self.sp_min.value(), self.sp_max.value())
        self._worker.progress.connect(self.pb.setValue)
        self._worker.done.connect(self._simpl_done)
        self._worker.start()

    def _simpl_done(self, result, stats):
        self.simplified = result
        QgsProject.instance().addMapLayer(result)
        txt = (f"{stats['features']} feições  ·  "
               f"{stats['before']:,} → {stats['after']:,} vértices  (-{stats['pct']:.1f}%)")
        if stats.get('clamped'):
            txt += f"  ·  {stats['clamped']} revertidas (abaixo do mínimo)"
        if stats.get('over_max'):
            txt += f"  ·  ⚠ {stats['over_max']} acima do máximo"
        self.lbl_res.setText(txt)
        self.status.setText("Simplificação concluída.")

    # ── Lógica aba 2 ──────────────────────────────────────────
    def _active_layer(self):
        """Retorna simplified se existir, senão a camada selecionada no combo."""
        return self.simplified or self._cur_layer()

    def _ensure_fields(self, lyr):
        existing = [f.name() for f in lyr.fields()]
        extras = [QgsField(fn, QVariant.String, "String", 100)
                  for fn in ["cliente", "fazenda", "talhao", "tipo_linha"]
                  if fn not in existing]
        if extras:
            lyr.dataProvider().addAttributes(extras)
            lyr.updateFields()

    def _load_table(self):
        lyr = self._active_layer()
        if not lyr:
            QMessageBox.warning(self, "AgroExport", "Nenhuma camada disponível.")
            return
        self._ensure_fields(lyr)
        self.tbl.setRowCount(0)
        for feat in lyr.getFeatures():
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            id_item = QTableWidgetItem(str(feat.id()))
            id_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tbl.setItem(r, 0, id_item)
            for col, fn in enumerate(["cliente", "fazenda", "talhao"], 1):
                v = feat[fn] if fn in feat.fields().names() else ""
                self.tbl.setItem(r, col, QTableWidgetItem(str(v or "")))
            combo = QComboBox()
            combo.addItems(TIPOS)
            cur = feat["tipo_linha"] if "tipo_linha" in feat.fields().names() else "Curva"
            combo.setCurrentIndex(max(combo.findText(str(cur or "Curva")), 0))
            self.tbl.setCellWidget(r, 4, combo)
        self.status.setText(f"{self.tbl.rowCount()} feições carregadas.")

    def _batch_fill(self):
        cliente = self.le_cliente.text().strip()
        fazenda = self.le_fazenda.text().strip()
        talhao  = self.le_talhao.text().strip()
        for row in range(self.tbl.rowCount()):
            if cliente:
                self.tbl.setItem(row, 1, QTableWidgetItem(cliente))
            if fazenda:
                self.tbl.setItem(row, 2, QTableWidgetItem(fazenda))
            if talhao:
                self.tbl.setItem(row, 3, QTableWidgetItem(talhao))

    def _apply(self):
        lyr = self._active_layer()
        if not lyr or self.tbl.rowCount() == 0:
            QMessageBox.warning(self, "AgroExport", "Carregue as feições primeiro.")
            return
        self._ensure_fields(lyr)
        from qgis.core import edit
        with edit(lyr):
            for row in range(self.tbl.rowCount()):
                fid = int(self.tbl.item(row, 0).text())
                for col, fn in enumerate(["cliente", "fazenda", "talhao"], 1):
                    lyr.changeAttributeValue(
                        fid, lyr.fields().indexOf(fn),
                        self.tbl.item(row, col).text()
                    )
                combo = self.tbl.cellWidget(row, 4)
                lyr.changeAttributeValue(
                    fid, lyr.fields().indexOf("tipo_linha"),
                    combo.currentText()
                )
        self.status.setText("Classificação salva → vá para Exportação.")

    # ── Lógica aba 3 ──────────────────────────────────────────
    def _export(self):
        lyr = self._active_layer()
        if not lyr:
            QMessageBox.warning(self, "AgroExport", "Nenhuma camada disponível.")
            return
        out = self.le_out.text().strip()
        if not out or not os.path.isdir(out):
            QMessageBox.warning(self, "AgroExport", "Selecione uma pasta de saída válida.")
            return
        lines = collect_lines(lyr)
        if not lines:
            QMessageBox.warning(self, "AgroExport", "Nenhuma feição válida encontrada.")
            return

        fb_client = lines[0]["cliente"] or "Cliente"
        fb_farm   = lines[0]["fazenda"] or "Fazenda"
        fb_field  = lines[0]["talhao"]  or "Talhao"
        for gl in lines:
            if not gl["cliente"]: gl["cliente"] = fb_client
            if not gl["fazenda"]: gl["fazenda"] = fb_farm
            if not gl["talhao"]:  gl["talhao"]  = fb_field

        msgs = []

        if self.chk_jd.isChecked():
            try:
                zip_path, n_lines, n_groups = export_jd_zip(
                    lines, out, "JohnDeere_GEN4",
                    fb_client, fb_farm, fb_field
                )
                msgs.append(
                    f"✅ John Deere GEN4:\n"
                    f"   {zip_path}\n"
                    f"   {n_lines} linhas · {n_groups} grupo(s)\n"
                    f"   → operationscenter.deere.com › Mapa › Importar"
                )
            except Exception as e:
                msgs.append(f"❌ Erro GEN4: {e}")
            try:
                zip_path = export_gs3_zip(
                    lines, out, "JohnDeere",
                    fb_client, fb_farm, fb_field
                )
                msgs.append(
                    f"✅ John Deere GS3_2630:\n"
                    f"   {zip_path}\n"
                    f"   → copiar para cartão SD › GS3_2630/"
                )
            except Exception as e:
                msgs.append(f"❌ Erro GS3_2630: {e}")

        if self.chk_ptx.isChecked():
            try:
                zip_path = export_agdata_zip(
                    lines, out, "PTX",
                    fb_client, fb_farm, fb_field
                )
                msgs.append(
                    f"✅ PTX Trimble AgData:\n"
                    f"   {zip_path}\n"
                    f"   → USB › AgData/Fields/ › monitor › Importar"
                )
            except Exception as e:
                msgs.append(f"❌ Erro AgData: {e}")
            try:
                zip_path = export_aggps_zip(
                    lines, out, "PTX",
                    fb_client, fb_farm, fb_field
                )
                msgs.append(
                    f"✅ PTX Trimble AgGPS:\n"
                    f"   {zip_path}\n"
                    f"   → cartão SD › AgGPS/Data/"
                )
            except Exception as e:
                msgs.append(f"❌ Erro AgGPS: {e}")

        self.lbl_exp.setText("\n".join(msgs))
        self.status.setText("Exportação concluída.")
        QMessageBox.information(self, "AgroExport — Concluído", "\n".join(msgs))

