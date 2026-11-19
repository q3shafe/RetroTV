# -*- mode: python ; coding: utf-8 -*-
# RetroTV PyInstaller spec file
# Usage: pyinstaller retrotv.spec

a = Analysis(
    ['tv.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Bundle the icon files so they're accessible at runtime
        ('retrotv.ico', '.'),
        ('retrotv.png', '.'),
        # Include config/playlist files if they exist alongside the script
        # ('tv_config.json', '.'),
        # ('user_config.json', '.'),
    ],
    hiddenimports=['vlc',
                   # Phone remote — imported lazily inside RetroTV._remote_start /
                   # _remote_show_dialog, so list them explicitly
                   'remote_control', 'qrcode',
                   # Windows media overlay (SMTC) — imported lazily in a try block
                   'winsdk', 'winsdk._winrt', 'winsdk.windows.media',
                   'winsdk.windows.media.playback', 'winsdk.windows.foundation'],
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
    a.binaries,
    a.datas,
    [],
    name='RetroTV',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='retrotv.ico',     # Windows EXE icon + taskbar icon
)
