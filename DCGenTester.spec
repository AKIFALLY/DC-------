# -*- mode: python ; coding: utf-8 -*-
# DC 發電機量測系統 — PyInstaller 打包設定 (onefile)
#   build: python -m PyInstaller DCGenTester.spec --noconfirm
#   產出 : dist/DCGenTester.exe  (config.yaml 需放在 exe 旁邊，可現場編輯)
block_cipher = None

a = Analysis(
    ['ui/main_window.py'],
    pathex=['.'],                       # 專案根 → 找得到 pel5000c / keyence / gpm8310 / utils
    binaries=[],
    datas=[('ui/assets/logo.png', 'assets'), ('ui/assets/app.ico', 'assets')],   # logo + icon 收進 bundle
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pandas', 'openpyxl', 'tkinter', 'pytest'],  # UI 未用，排除以縮小體積
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='DCGenTester',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # 視窗程式，不開主控台
    disable_windowed_traceback=False,
    icon='ui/assets/app.ico',
)
