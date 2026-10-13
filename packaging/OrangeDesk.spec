# One installed directory: no per-launch extraction; Qt DLLs stay replaceable.
from pathlib import Path
root = Path(SPECPATH).parent
exclude = ['PySide6.QtQml','PySide6.QtQuick','PySide6.QtQuickWidgets','PySide6.QtWebEngineCore',
           'PySide6.QtWebEngineWidgets','PySide6.QtWebEngineQuick','PySide6.QtMultimedia',
           'PySide6.QtMultimediaWidgets','PySide6.Qt3DCore','PySide6.QtCharts',
           'PySide6.QtDataVisualization','PySide6.QtPdf','PySide6.QtPdfWidgets',
           'PySide6.QtTest','tkinter','pytest','numpy','pandas','matplotlib']
gui = Analysis([str(root/'app/gui_entry.py')], pathex=[str(root)],
    datas=[(str(root/'resources'),'resources'),(str(root/'licenses'),'licenses'),(str(root/'app/assets'),'app/assets')],
    hiddenimports=['app.ui','app.engine','app.install_support'], excludes=exclude, noarchive=False)
console = Analysis([str(root/'app/cli_entry.py')],pathex=[str(root)],
    hiddenimports=['app.cli','app.install_support'],excludes=['PySide6','shiboken6',*exclude],noarchive=False)
# Qt 6.12 uses the Windows ICU API. The build interpreter also contains an
# incompatible ICU 78 with version-suffixed exports; never shadow System32 ICU.
for analysis in (gui, console):
    analysis.binaries = [entry for entry in analysis.binaries
        if not Path(entry[0]).name.lower().startswith(('icuuc.', 'icudt', 'icu.'))]
gui_pyz = PYZ(gui.pure)
cli_pyz = PYZ(console.pure)
gui_exe = EXE(gui_pyz,gui.scripts,[],exclude_binaries=True,name='OrangeDesk',console=False,
    icon=str(root/'.build/brand/icon.ico'),version=str(root/'packaging/version.txt'),upx=False,
    contents_directory='_internal',disable_windowed_traceback=False)
cli_exe = EXE(cli_pyz,console.scripts,[],exclude_binaries=True,name='orange',console=True,
    icon=str(root/'.build/brand/icon.ico'),version=str(root/'packaging/version.txt'),upx=False,
    contents_directory='_internal')
bundle = COLLECT(gui_exe,cli_exe,gui.binaries,gui.datas,console.binaries,console.datas,
    strip=False,upx=False,name='OrangeDesk')
