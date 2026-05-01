def classFactory(iface):
    from .plugin import AgroExportPlugin
    return AgroExportPlugin(iface)
