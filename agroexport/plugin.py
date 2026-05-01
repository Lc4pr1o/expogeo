# plugin.py
import os
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsVectorLayer, QgsWkbTypes, QgsProject


class AgroExportPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, 'icons', 'icon.png')
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, 'AgroExport — Linhas-guia', self.iface.mainWindow())
        self.action.setToolTip('Exportar linhas de orientação para terminais agrícolas')
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('&AgroExport', self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu('&AgroExport', self.action)
        del self.action

    def run(self):
        has_lines = any(
            isinstance(lyr, QgsVectorLayer) and
            lyr.geometryType() == QgsWkbTypes.LineGeometry
            for lyr in QgsProject.instance().mapLayers().values()
        )
        if not has_lines:
            QMessageBox.warning(
                self.iface.mainWindow(), 'AgroExport',
                'Nenhuma camada de linhas encontrada no projeto.\n\n'
                'Adicione uma camada de linhas (LineString) antes de exportar.'
            )
            return

        from .dialog import AgroDialog
        dlg = AgroDialog()
        dlg.exec()
