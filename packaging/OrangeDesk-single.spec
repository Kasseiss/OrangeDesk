from pathlib import Path
root = Path(SPECPATH).parent
exclude = ['PySide6.QtQml','PySide6.QtQuick','PySide6.QtQuickWidgets','PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineWidgets','PySide6.QtWebEngineQuick','PySide6.QtMultimedia',
    'PySide6.QtMultimediaWidgets','PySide6.Qt3DCore','PySide6.QtCharts',
    'PySide6.QtDataVisualization','PySide6.QtPdf','PySide6.QtPdfWidgets',
    'PySide6.QtTest','tkinter','pytest','numpy','pandas','matplotlib']
analysis = Analysis([str(root/'app/single_entry.py')], pathex=[str(root)],
    datas=[(str(root/'resources'),'resources'), (str(root/'licenses'),'licenses'),
           (str(root/'app/assets'),'app/assets')],
    hiddenimports=['app.ui','app.engine','app.cli','app.install_support'], excludes=exclude, noarchive=False)
analysis.binaries = [entry for entry in analysis.binaries
    if not Path(entry[0]).name.lower().startswith(('icuuc.', 'icudt', 'icu.'))]
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas, [],
    name='OrangeDesk', console=False, icon=str(root/'.build/brand/icon.ico'),
    version=str(root/'packaging/version.txt'), upx=False, disable_windowed_traceback=False)
