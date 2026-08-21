# Mizuki 控制台 PyInstaller 打包配置（onedir + 无控制台窗口）
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('static', 'static'), ('icon.jpg', '.'), ('icon.ico', '.')],
    hiddenimports=[
        'webview.platforms.winforms',
        'pystray._win32',
        *collect_submodules('uvicorn'),
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Mizuki',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='icon.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Mizuki',
)
