# Directory bundle: no extraction on every launch, no Python installation required.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH)
a = Analysis(
    [str(root / 'scripts/windows_entry.py')],
    pathex=[str(root / 'src')],
    binaries=[],
    datas=collect_data_files('selark') + collect_data_files('tzdata') + copy_metadata('selark'),
    hiddenimports=['win32timezone'] + collect_submodules('uvicorn') + collect_submodules('selark', filter=lambda name: name != 'selark.platforms.posix'),
    hookspath=[],
    excludes=['pytest', 'tkinter', 'unittest', 'selark.platforms.posix'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [('X utf8', None, 'OPTION'), ('u', None, 'OPTION')], exclude_binaries=True, name='selark-core',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, hide_console='hide-early', disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='selark-core')
