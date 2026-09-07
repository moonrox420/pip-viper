# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification file for compiling PipViper IDE into a standalone executable."""

from pathlib import Path
import sys

block_cipher = None
project_root = Path.cwd().resolve()

# Hidden imports to ensure dynamic imports (Jedi, Pydantic, standard library tools) are bundled
hidden_imports = [
    # PySide6 Core & Widgets
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    # Language Tooling & AST
    "jedi",
    "pydantic",
    "black",
    "isort",
    "autoflake",
    # CPython Internals & Profiler Subsystems
    "cProfile",
    "pstats",
    "tracemalloc",
    "dis",
    "symtable",
    "ast",
    "difflib",
    # Operating System & Memory
    "ctypes",
    "ctypes.wintypes",
    # PipViper Internal Modules
    "src",
    "src.app",
    "src.editor",
    "src.panels",
    "src.widgets",
    "src.internals",
    "src.profiler_visualizer",
    "src.memory_tracker",
    "src.environment",
    "src.dependencies",
    "src.vcs",
    "src.diff_viewer",
    "src.diagnostics",
    "src.testing",
    "src.repl_harness",
    "src.code_tools",
    "src.debug_harness",
    "src.navigation",
    "src.pip_viper",
    "src.styles",
]

# Asset data files to bundle
datas = [
    ("README.md", "."),
]

a = Analysis(
    ["launcher.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "turtle", "test", "unittest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PipViper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # Windowed GUI application (no terminal popup)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PipViper",
)
