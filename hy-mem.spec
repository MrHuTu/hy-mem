# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import copy_metadata

datas = []
hiddenimports = ['store', 'config', 'tui', 'presets', 'setup_wizard', 'menu', 'storage_ops', 'install_ops', 'skill_ops', 'mcp_server', 'helptext', 'local_embedder', 'runtime_check', 'netprobe', 'vdb_endpoint']
datas += collect_data_files('chromadb')
datas += copy_metadata('hy-memory')
datas += copy_metadata('chromadb')
hiddenimports += collect_submodules('hy_memory')
hiddenimports += collect_submodules('chromadb')
hiddenimports += collect_submodules('fastapi')
hiddenimports += collect_submodules('uvicorn')
hiddenimports += collect_submodules('opentelemetry.instrumentation.fastapi')


a = Analysis(
    ['cli/_pyi_entry.py'],
    pathex=['cli'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'IPython', 'pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='hy-mem',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='hy-mem',
)
