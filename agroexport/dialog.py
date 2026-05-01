# dialog.py
# Diálogo principal do plugin AgroExport.

import os

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QLabel, QComboBox, QDoubleSpinBox, QSpinBox, QPushButton, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox,
    QLineEdit, QGroupBox, QProgressBar, QMessageBox, QSizePolicy,
    QFormLayout, QStyledItemDelegate, QApplication, QScrollArea,
    QListWidget, QAbstractItemView
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
    export_gs3_zip, ascii_safe,
    BLOCK_SIZE_LIMIT_MB, estimate_layer_size_mb, estimate_lines_size_mb,
    split_into_blocks,
)

TIPOS = ["Curva", "AB", "Limite"]
NOMENCLATURAS = ["Parte", "Bloco", "Gleba"]


def _dir_size_mb(path):
    """Soma o tamanho em MB de todos os arquivos em um diretório."""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for fname in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fname))
            except OSError:
                pass
    return total / (1024 * 1024)


class TipoDelegate(QStyledItemDelegate):
    """Mostra QComboBox apenas durante a edição; célula em repouso é texto puro."""

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.addItems(TIPOS)
        return combo

    def setEditorData(self, editor, index):
        val = index.data(Qt.ItemDataRole.DisplayRole) or "Curva"
        editor.setCurrentIndex(max(editor.findText(val), 0))

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.ItemDataRole.DisplayRole)


# ── Worker thread ──────────────────────────────────────────────

class Worker(QThread):
    progress = pyqtSignal(int)
    done     = pyqtSignal(object, dict)

    def __init__(self, layer, min_dist, max_dist, dev_tol):
        super().__init__()
        self.layer    = layer
        self.min_dist = min_dist
        self.max_dist = max_dist
        self.dev_tol  = dev_tol

    def run(self):
        result, stats = simplify_layer(
            self.layer, self.min_dist, self.max_dist, self.dev_tol, self.progress.emit
        )
        self.done.emit(result, stats)


# ── Pré-visualização de blocos ─────────────────────────────────

class BlockPreviewDialog(QDialog):
    """Mostra blocos auto-gerados; permite reorganizar talhões via drag-and-drop."""

    def __init__(self, blocks, talhao_to_lines, nomenclature, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Divisão em blocos — Pré-visualização")
        self.setMinimumSize(700, 420)
        self.resize(800, 480)
        self._talhao_to_lines = talhao_to_lines
        self._nomenclature = nomenclature
        self._lists = []
        self._build(blocks)

    def _build(self, blocks):
        L = QVBoxLayout(self)

        n = len(blocks)
        info = QLabel(
            f"O projeto excede {BLOCK_SIZE_LIMIT_MB} MB e será exportado em "
            f"<b>{n} bloco(s)</b>. "
            "Arraste talhões entre as colunas para reorganizar antes de exportar."
        )
        info.setWordWrap(True)
        L.addWidget(info)

        cols_widget = QWidget()
        cols_layout = QHBoxLayout(cols_widget)
        cols_layout.setSpacing(8)

        for block in blocks:
            warn = " ⚠ acima do limite" if block.get('oversized') else ""
            header = QLabel(
                f"<b>{block['name']}</b><br>"
                f"{block['size_mb']:.1f} MB{warn}"
            )
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)

            lst = QListWidget()
            lst.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
            lst.setDefaultDropAction(Qt.DropAction.MoveAction)
            lst.setMinimumWidth(140)
            lst.setMaximumWidth(240)
            for talhao in block['talhoes']:
                lst.addItem(talhao)

            col = QVBoxLayout()
            col.addWidget(header)
            col.addWidget(lst)
            grp = QGroupBox()
            grp.setLayout(col)
            cols_layout.addWidget(grp)
            self._lists.append(lst)

        scroll = QScrollArea()
        scroll.setWidget(cols_widget)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        L.addWidget(scroll)

        btns = QHBoxLayout()
        btn_ok = QPushButton("Confirmar e Exportar")
        btn_ok.setStyleSheet("font-weight:bold;padding:8px")
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancelar")
        btn_cancel.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(btn_ok)
        btns.addWidget(btn_cancel)
        L.addLayout(btns)

    def get_final_blocks(self):
        """Retorna blocos com linhas conforme estado atual das listas."""
        result = []
        for i, lst in enumerate(self._lists, 1):
            block_lines = []
            for j in range(lst.count()):
                talhao = lst.item(j).text()
                block_lines.extend(self._talhao_to_lines.get(talhao, []))
            if block_lines:
                result.append({
                    'name': f'{self._nomenclature} {i}',
                    'lines': block_lines,
                })
        return result


# ── Diálogo principal ─────────────────────────────────────────

class AgroDialog(QDialog):
    def __init__(self):
        super().__init__(iface.mainWindow())
        self.setWindowTitle("AgroExport")
        self.setMinimumSize(580, 460)
        self.resize(700, 600)
        self.simplified = None
        self._import_paths = []
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_import(), "1 · Importação")
        self.tabs.addTab(self._tab_simpl(),  "2 · Padronização")
        self.tabs.addTab(self._tab_class(),  "3 · Classificação")
        self.tabs.addTab(self._tab_export(), "4 · Exportação")
        root.addWidget(self.tabs)
        self.status = QLabel("Pronto.")
        self.status.setStyleSheet("color:gray;font-size:11px")
        root.addWidget(self.status)

        # Desabilita abas downstream enquanto nenhuma camada está disponível
        has_layers = self.cb.count() > 0
        for i in range(1, 4):
            self.tabs.setTabEnabled(i, has_layers)

    # ── Aba 1: Padronização ─────────────────────────────────────
    def _tab_simpl(self):
        w = QWidget()
        L = QVBoxLayout(w)

        g1 = QGroupBox("Camada de entrada")
        r1 = QVBoxLayout(g1)
        row_cb = QHBoxLayout()
        row_cb.addWidget(QLabel("Camada:"))
        self.cb = QComboBox()
        self.cb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for lid, lyr in QgsProject.instance().mapLayers().items():
            if isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == QgsWkbTypes.LineGeometry:
                self.cb.addItem(lyr.name(), lid)
        row_cb.addWidget(self.cb)
        r1.addLayout(row_cb)

        # Melhoria 4: indicador de tamanho do projeto
        self.lbl_size = QLabel("")
        self.lbl_size.setStyleSheet("color:gray;font-size:11px")
        r1.addWidget(self.lbl_size)
        L.addWidget(g1)

        self.cb.currentIndexChanged.connect(self._update_size_label)
        if self.cb.count() > 0:
            self._update_size_label()

        g2 = QGroupBox("Simplificação (redução de vértices redundantes)")
        r2 = QHBoxLayout(g2)
        r2.addWidget(QLabel("Tolerância geométrica:"))
        self.sp_tol = QDoubleSpinBox()
        self.sp_tol.setRange(0.0, 1.0)
        self.sp_tol.setValue(0.05)
        self.sp_tol.setDecimals(2)
        self.sp_tol.setSingleStep(0.01)
        self.sp_tol.setSuffix(" m")
        self.sp_tol.setToolTip(
            "Desvio máximo permitido ao remover vértices próximos.\n"
            "0.05 m = apenas vértices quase colineares são removidos.\n"
            "0.00 m = nenhum vértice é removido (só densificação)."
        )
        r2.addWidget(self.sp_tol)
        r2.addStretch()
        L.addWidget(g2)

        g3 = QGroupBox("Espaçamento entre vértices (m)")
        r3 = QHBoxLayout(g3)
        r3.addWidget(QLabel("Mínimo:"))
        self.sp_min = QSpinBox()
        self.sp_min.setRange(1, 100)
        self.sp_min.setValue(5)
        self.sp_min.setSuffix(" m")
        self.sp_min.setToolTip(
            "Vértices mais próximos que este valor serão removidos\n"
            "SOMENTE se o desvio geométrico for menor que a tolerância acima.\n"
            "Vértices em curvas são sempre preservados."
        )
        r3.addWidget(self.sp_min)
        r3.addSpacing(16)
        r3.addWidget(QLabel("Máximo:"))
        self.sp_max = QSpinBox()
        self.sp_max.setRange(2, 500)
        self.sp_max.setValue(15)
        self.sp_max.setSuffix(" m")
        self.sp_max.setToolTip(
            "Segmentos maiores que este valor receberão vértices\n"
            "interpolados automaticamente. Não altera o traçado."
        )
        r3.addWidget(self.sp_max)
        r3.addStretch()
        L.addWidget(g3)

        self.pb = QProgressBar()
        self.pb.setValue(0)
        L.addWidget(self.pb)

        btn_run = QPushButton("Padronizar")
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
        self.tbl.setItemDelegateForColumn(4, TipoDelegate(self.tbl))
        L.addWidget(self.tbl)

        # Preenchimento em lote
        g_batch = QGroupBox("Preenchimento em lote")
        fb = QFormLayout(g_batch)
        self.le_cliente = QLineEdit()
        self.le_fazenda = QLineEdit()

        # Melhoria 1: Talhão vira seletor de nomenclatura de bloco
        self.cb_talhao = QComboBox()
        self.cb_talhao.addItems(NOMENCLATURAS)
        self.cb_talhao.setToolTip(
            "Define o nome base dos blocos na exportação.\n"
            "Parte 1, Parte 2… / Bloco 1, Bloco 2… / Gleba 1, Gleba 2…"
        )

        fb.addRow("Cliente:", self.le_cliente)
        fb.addRow("Fazenda:", self.le_fazenda)
        fb.addRow("Talhão:", self.cb_talhao)
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

    # ── Aba 1: Importação ───────────────────────────────────────
    def _tab_import(self):
        w = QWidget()
        L = QVBoxLayout(w)

        g1 = QGroupBox("Linhas de colheita (.shp)")
        r1 = QHBoxLayout(g1)
        self.le_import = QLineEdit()
        self.le_import.setReadOnly(True)
        self.le_import.setPlaceholderText("Selecione um ou mais arquivos .shp…")
        r1.addWidget(self.le_import)
        btn_browse = QPushButton("Procurar…")
        btn_browse.clicked.connect(self._browse_shp)
        r1.addWidget(btn_browse)
        L.addWidget(g1)

        btn_import = QPushButton("Importar")
        btn_import.setStyleSheet("font-weight:bold;padding:8px;font-size:13px")
        btn_import.clicked.connect(self._import_shp)
        L.addWidget(btn_import)

        self.lbl_import_res = QLabel("")
        self.lbl_import_res.setWordWrap(True)
        L.addWidget(self.lbl_import_res)
        L.addStretch()
        return w

    # ── Lógica aba 1 ──────────────────────────────────────────
    def _cur_layer(self):
        lid = self.cb.currentData()
        return QgsProject.instance().mapLayer(lid) if lid else None

    def _update_size_label(self):
        lyr = self._cur_layer()
        if not lyr:
            self.lbl_size.setText("")
            return
        mb = estimate_layer_size_mb(lyr)
        if mb > BLOCK_SIZE_LIMIT_MB:
            self.lbl_size.setStyleSheet("color:orange;font-size:11px")
            self.lbl_size.setText(
                f"Tamanho estimado: {mb:.1f} MB — exportação será dividida em blocos"
            )
        else:
            self.lbl_size.setStyleSheet("color:gray;font-size:11px")
            self.lbl_size.setText(f"Tamanho estimado: {mb:.1f} MB")

    def _run_simpl(self):
        lyr = self._cur_layer()
        if not lyr:
            QMessageBox.warning(self, "AgroExport", "Selecione uma camada.")
            return
        if self.sp_min.value() >= self.sp_max.value():
            QMessageBox.warning(self, "AgroExport",
                                "O espaçamento mínimo deve ser menor que o máximo.")
            return
        self.pb.setValue(0)
        self.lbl_res.setText("Processando…")
        self._worker = Worker(lyr, self.sp_min.value(), self.sp_max.value(), self.sp_tol.value())
        self._worker.progress.connect(self.pb.setValue)
        self._worker.done.connect(self._simpl_done)
        self._worker.start()

    def _simpl_done(self, result, stats):
        self.simplified = result
        QgsProject.instance().addMapLayer(result)
        diff = stats['after'] - stats['before']
        sinal = "+" if diff > 0 else ""
        mb = estimate_layer_size_mb(result)
        txt = (f"{stats['features']} feições  ·  "
               f"{stats['before']:,} → {stats['after']:,} vértices  ({sinal}{diff:,})  ·  {mb:.1f} MB")
        self.lbl_res.setText(txt)
        self.status.setText("Padronização concluída.")

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
        self.status.setText("Carregando feições…")
        QApplication.processEvents()

        feats = list(lyr.getFeatures())
        field_names = {f.name() for f in lyr.fields()}

        self.tbl.setUpdatesEnabled(False)
        self.tbl.blockSignals(True)
        self.tbl.setRowCount(len(feats))

        for r, feat in enumerate(feats):
            id_item = QTableWidgetItem(str(feat.id()))
            id_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tbl.setItem(r, 0, id_item)
            for col, fn in enumerate(["cliente", "fazenda", "talhao"], 1):
                v = feat[fn] if fn in field_names else ""
                self.tbl.setItem(r, col, QTableWidgetItem(str(v or "")))
            tipo = feat["tipo_linha"] if "tipo_linha" in field_names else "Curva"
            self.tbl.setItem(r, 4, QTableWidgetItem(str(tipo or "Curva")))

        self.tbl.blockSignals(False)
        self.tbl.setUpdatesEnabled(True)
        self.status.setText(f"{len(feats)} feições carregadas.")

    def _batch_fill(self):
        cliente = self.le_cliente.text().strip()
        fazenda = self.le_fazenda.text().strip()
        talhao  = self.cb_talhao.currentText()
        for row in range(self.tbl.rowCount()):
            if cliente:
                self.tbl.setItem(row, 1, QTableWidgetItem(cliente))
            if fazenda:
                self.tbl.setItem(row, 2, QTableWidgetItem(fazenda))
            self.tbl.setItem(row, 3, QTableWidgetItem(talhao))

    def _apply(self):
        lyr = self._active_layer()
        if not lyr or self.tbl.rowCount() == 0:
            QMessageBox.warning(self, "AgroExport", "Carregue as feições primeiro.")
            return
        self._ensure_fields(lyr)
        idx_cli  = lyr.fields().indexOf("cliente")
        idx_faz  = lyr.fields().indexOf("fazenda")
        idx_tal  = lyr.fields().indexOf("talhao")
        idx_tipo = lyr.fields().indexOf("tipo_linha")
        from qgis.core import edit
        with edit(lyr):
            for row in range(self.tbl.rowCount()):
                fid = int(self.tbl.item(row, 0).text())
                lyr.changeAttributeValue(fid, idx_cli,  self.tbl.item(row, 1).text())
                lyr.changeAttributeValue(fid, idx_faz,  self.tbl.item(row, 2).text())
                lyr.changeAttributeValue(fid, idx_tal,  self.tbl.item(row, 3).text())
                lyr.changeAttributeValue(fid, idx_tipo, self.tbl.item(row, 4).text())
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
        for gl in lines:
            if not gl["cliente"]: gl["cliente"] = fb_client
            if not gl["fazenda"]: gl["fazenda"] = fb_farm
            if not gl["talhao"]:  gl["talhao"]  = gl["name"] or "Talhao"

        nomenclature = self.cb_talhao.currentText()
        total_mb = estimate_lines_size_mb(lines)

        if total_mb > BLOCK_SIZE_LIMIT_MB:
            # Mapa talhão → linhas para o diálogo de preview
            talhao_to_lines = {}
            for gl in lines:
                key = gl['talhao'] or gl['fazenda'] or gl['name'] or '?'
                talhao_to_lines.setdefault(key, []).append(gl)

            blocks = split_into_blocks(lines, nomenclature)

            oversized = [b for b in blocks if b.get('oversized')]
            if oversized:
                names = ", ".join(b['name'] for b in oversized)
                QMessageBox.warning(
                    self, "AgroExport",
                    f"Os seguintes blocos excedem {BLOCK_SIZE_LIMIT_MB} MB "
                    f"individualmente e não podem ser subdivididos:\n{names}"
                )

            dlg = BlockPreviewDialog(blocks, talhao_to_lines, nomenclature, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            export_blocks = dlg.get_final_blocks()
        else:
            export_blocks = [{'name': None, 'lines': lines}]

        self.status.setText("Exportando... aguarde.")
        QApplication.processEvents()

        errors = []
        total_lines_exported = 0
        total_disk_mb = 0.0

        for block in export_blocks:
            block_lines = block['lines']
            block_name  = block['name']
            field_label = block_name or fb_farm
            block_dir   = os.path.join(out, block_name) if block_name else out
            if block_name:
                os.makedirs(block_dir, exist_ok=True)

            if self.chk_jd.isChecked():
                try:
                    export_jd_zip(block_lines, block_dir, "JohnDeere_GEN4",
                                  fb_client, fb_farm, field_label)
                except Exception as e:
                    errors.append(f"GEN4: {e}")
                try:
                    export_gs3_zip(block_lines, block_dir, "JohnDeere",
                                   fb_client, fb_farm, field_label)
                except Exception as e:
                    errors.append(f"GS3_2630: {e}")

            if self.chk_ptx.isChecked():
                try:
                    export_agdata_zip(block_lines, block_dir, "PTX",
                                      fb_client, fb_farm, field_label)
                except Exception as e:
                    errors.append(f"AgData: {e}")
                try:
                    export_aggps_zip(block_lines, block_dir, "PTX",
                                     fb_client, fb_farm, field_label)
                except Exception as e:
                    errors.append(f"AgGPS: {e}")

            total_lines_exported += len(block_lines)
            total_disk_mb += _dir_size_mb(block_dir)

        n_blocks = len(export_blocks)
        if n_blocks > 1:
            bloco_info = (f"{n_blocks} blocos "
                          f"({nomenclature} 1 a {nomenclature} {n_blocks})")
        else:
            bloco_info = "1 arquivo"

        msg_parts = [
            f"Projeto: {fb_client} / {fb_farm}",
            f"Linhas exportadas: {total_lines_exported}",
            f"Blocos gerados: {bloco_info}",
            f"Tamanho em disco: {total_disk_mb:.1f} MB",
        ]
        if errors:
            msg_parts.append("\nErros:\n" + "\n".join(f"  {e}" for e in errors))

        msg = "\n".join(msg_parts)
        self.lbl_exp.setText(msg)
        self.status.setText("Exportação concluída.")
        QMessageBox.information(self, "AgroExport — Concluído", msg)

    # ── Lógica aba 1 ──────────────────────────────────────────
    def _browse_shp(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar shapefiles", "", "Shapefile (*.shp)"
        )
        if paths:
            self._import_paths = paths
            if len(paths) == 1:
                self.le_import.setText(paths[0])
            else:
                self.le_import.setText(f"{len(paths)} arquivos selecionados")

    def _import_shp(self):
        if not self._import_paths:
            QMessageBox.warning(self, "AgroExport",
                                "Selecione um ou mais arquivos .shp.")
            return

        imported_info = []
        errors = []
        total_mb = 0.0

        for path in self._import_paths:
            if not os.path.isfile(path):
                errors.append(f"Não encontrado: {os.path.basename(path)}")
                continue
            name = os.path.splitext(os.path.basename(path))[0]
            lyr = QgsVectorLayer(path, name, "ogr")
            if not lyr.isValid():
                errors.append(f"Inválido: {name}")
                continue
            QgsProject.instance().addMapLayer(lyr)
            if lyr.geometryType() == QgsWkbTypes.LineGeometry:
                self.cb.addItem(lyr.name(), lyr.id())
            mb = estimate_layer_size_mb(lyr)
            total_mb += mb
            imported_info.append(
                f"  {name}  —  {lyr.featureCount()} feições  ·  {mb:.1f} MB"
            )

        if not imported_info:
            msg = "Nenhuma camada foi importada."
            if errors:
                msg += "\n" + "\n".join(errors)
            QMessageBox.warning(self, "AgroExport", msg)
            return

        self.cb.setCurrentIndex(self.cb.count() - 1)

        lines = [f"Total importado: {total_mb:.1f} MB"] + imported_info
        if errors:
            lines += ["", "Erros:"] + [f"  {e}" for e in errors]
        self.lbl_import_res.setText("\n".join(lines))
        self.status.setText(
            f"{len(imported_info)} camada(s) importada(s) — prossiga para Padronização."
        )

        for i in range(1, 4):
            self.tabs.setTabEnabled(i, True)
        self.tabs.setCurrentIndex(1)
