# -*- mode: python ; coding: utf-8 -*-
# Linux build: pyinstaller retrotv-linux.spec  (run on Linux)
a = Analysis(
    ['tv.py'],
    pathex=[],
    binaries=[],
    datas=[('retrotv.png', '.')],
    hiddenimports=['vlc', 'remote_control', 'qrcode', 'retrotv_epg', 'dvr_scheduler'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['winsdk'],
    noarchive=False,
)
# Use the system libvlc (+ its plugins); a bundled libvlc without plugins
# makes vlc.Instance() return None.
a.binaries = [b for b in a.binaries if not b[0].startswith(('libvlc.so', 'libvlccore.so'))]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
    name='retrotv', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False)
