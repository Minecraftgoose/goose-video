# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['rec_tool.py'],
    pathex=[],
    binaries=[],
    datas=[('C:/Users/BayMax/AppData/Local/Programs/Python/Python311/Lib/site-packages/imageio_ffmpeg/binaries', 'imageio_ffmpeg/binaries')],
    hiddenimports=['pyaudiowpatch', 'mss', 'pystray'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='xiaoe_recorder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
