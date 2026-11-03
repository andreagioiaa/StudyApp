# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file build per StudyFlow."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata, get_package_paths

datas = [
    ("assets/app.ico", "assets"),
    ("assets/app.png", "assets"),
]
datas += collect_data_files("customtkinter")
# Su Windows zoneinfo/dateutil usano il pacchetto tzdata per i fusi orari:
# senza i suoi file .zi le ricorrenze ICS con fuso orario fallirebbero nell'exe.
datas += collect_data_files("tzdata")
# pywhispercpp.model legge la versione da importlib.metadata all'import:
# senza il dist-info l'import fallirebbe dentro l'exe one-file.
datas += copy_metadata("pywhispercpp")

# Il wheel pywhispercpp installa le DLL native nella radice di site-packages,
# non dentro il pacchetto. Vanno raccolte esplicitamente per la build.
binaries = []
try:
    _package_base, _package_dir = get_package_paths("pywhispercpp")
    _site_packages = Path(_package_dir).parent
    for _pattern in (
        "ggml-*.dll",
        "whisper-*.dll",
        "msvcp140-*.dll",
        "vcomp140-*.dll",
    ):
        for _dll in _site_packages.glob(_pattern):
            binaries.append((str(_dll), "."))
except Exception:
    pass

hiddenimports = [
    "matplotlib.backends.backend_tkagg",
    "matplotlib.backends._backend_tk",
    "psutil",
    "icalendar",
    "dateutil.rrule",
    "tzdata",
    "pygame",
    "yt_dlp",
    "yt_dlp_ejs",
    "yt_dlp_ejs.yt.solver",
    "pywhispercpp",
    "pywhispercpp.model",
    "_pywhispercpp",
    "numpy",
    "requests",
    "tqdm",
    "platformdirs",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": ["TkAgg"]}},
    runtime_hooks=[],
    excludes=[
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "wx",
        "IPython",
        "jupyter",
        "notebook",
        "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="StudyFlow",
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
    icon="assets/app.ico",
    version="version_info.txt",
    uac_admin=False,
)
