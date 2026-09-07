"""Virtual Environment Auto-Detection, Interpreter Management, and UI Selector.

This module provides:
    - Multi-engine Python environment discovery (.venv, uv, Poetry, Conda, System).
    - Lightweight, non-blocking version and metadata probing with pyvenv.cfg fast-path.
    - Status bar EnvironmentSelectorWidget.
    - Interactive EnvironmentPickerDialog with search and custom interpreter browsing.
"""

from __future__ import annotations

import enum
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Sequence

from PySide6.QtCore import QPoint, Qt, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QFont, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .pip_viper import ColorPalette

_LOGGER = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Data Models
# -----------------------------------------------------------------------------


class EnvironmentType(str, enum.Enum):
    """Classification of Python execution environments."""

    VENV = "venv"
    UV = "uv"
    POETRY = "poetry"
    CONDA = "conda"
    SYSTEM = "system"
    CUSTOM = "custom"


class PythonEnvironment:
    """Represents a discovered or configured Python interpreter environment."""

    def __init__(
        self,
        name: str,
        env_type: EnvironmentType,
        executable: Path,
        version: str = "",
        prefix: Path | None = None,
        is_active: bool = False,
        details: str = "",
    ) -> None:
        self.name: str = name
        self.env_type: EnvironmentType = env_type
        self.executable: Path = executable.resolve() if executable.exists() else executable
        self.version: str = version
        self.prefix: Path = prefix.resolve() if prefix and prefix.exists() else (executable.parent.parent if prefix is None else prefix)
        self.is_active: bool = is_active
        self.details: str = details

    @property
    def display_label(self) -> str:
        """Formatted human-readable label for UI selection."""
        ver = f"Python {self.version}" if self.version else "Python"
        return f"{ver} ({self.name}) [{self.env_type.value}]"

    def __repr__(self) -> str:
        return (
            f"PythonEnvironment(name={self.name!r}, type={self.env_type.value}, "
            f"ver={self.version!r}, exe={str(self.executable)!r}, active={self.is_active})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PythonEnvironment):
            return False
        return self.executable == other.executable


# -----------------------------------------------------------------------------
# Environment Detection Engine
# -----------------------------------------------------------------------------


class EnvironmentDetector:
    """Discovers installed Python interpreters and virtual environments across the system."""

    def __init__(self, project_root: Path | None = None) -> None:
        self._project_root: Path | None = project_root
        self._version_cache: dict[str, str] = {}

    def set_project_root(self, project_root: Path | None) -> None:
        self._project_root = project_root

    def discover_environments(self, project_root: Path | None = None) -> list[PythonEnvironment]:
        """Auto-detect all available Python environments across supported engines.

        Returns a deduplicated list of discovered environments, prioritizing local
        project virtual environments first.
        """
        root = project_root or self._project_root
        environments: list[PythonEnvironment] = []
        seen_executables: set[Path] = set()

        def add_env(env: PythonEnvironment) -> None:
            norm_exe = env.executable.resolve() if env.executable.exists() else env.executable
            if norm_exe not in seen_executables:
                seen_executables.add(norm_exe)
                environments.append(env)

        # 1. Project Local Virtual Environments (.venv, venv, env, .env)
        if root and root.is_dir():
            for candidate_name in (".venv", "venv", "env", ".env"):
                candidate_dir = root / candidate_name
                env = self._probe_venv_dir(candidate_dir, name=candidate_name, env_type=EnvironmentType.VENV)
                if env:
                    # Check if uv created it (uv.lock presence or pyvenv.cfg mentioning uv)
                    if (root / "uv.lock").exists() or self._is_uv_env(candidate_dir):
                        env.env_type = EnvironmentType.UV
                        env.details = "Project local uv virtual environment"
                    else:
                        env.details = "Project local virtual environment"
                    add_env(env)

        # 2. Poetry Virtual Environments
        for poetry_dir in self._get_poetry_virtualenv_dirs():
            if poetry_dir.is_dir():
                for sub in poetry_dir.iterdir():
                    if sub.is_dir():
                        env = self._probe_venv_dir(sub, name=sub.name, env_type=EnvironmentType.POETRY)
                        if env:
                            env.details = f"Poetry virtualenv ({sub.name})"
                            add_env(env)

        # 3. Conda / Mamba Environments
        for conda_env in self._discover_conda_environments():
            add_env(conda_env)

        # 4. System Interpreters (Windows py launcher or PATH)
        for sys_env in self._discover_system_interpreters():
            add_env(sys_env)

        # 5. Current Host Process Python
        host_exe = Path(sys.executable)
        host_env = self._probe_venv_dir(host_exe.parent.parent, name="Current IDE Python", env_type=EnvironmentType.SYSTEM)
        if not host_env:
            ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            host_env = PythonEnvironment(
                name="IDE Host Python",
                env_type=EnvironmentType.SYSTEM,
                executable=host_exe,
                version=ver,
                prefix=Path(sys.prefix),
                details="Interpreter executing the PipViper IDE application",
            )
        add_env(host_env)

        return environments

    def probe_python_version(self, executable: Path) -> str:
        """Probe the python version string for an executable with fast pyvenv.cfg lookup."""
        exe_str = str(executable)
        if exe_str in self._version_cache:
            return self._version_cache[exe_str]

        # Fast path: check pyvenv.cfg in parent or grandparent
        for parent_dir in (executable.parent, executable.parent.parent):
            cfg = parent_dir / "pyvenv.cfg"
            if cfg.is_file():
                try:
                    for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if line.startswith("version =") or line.startswith("version_info ="):
                            v = line.split("=", 1)[1].strip().split()[0]
                            self._version_cache[exe_str] = v
                            return v
                except Exception:
                    pass

        # Fallback: execute python --version
        if executable.is_file():
            try:
                kwargs: dict[str, Any] = {
                    "capture_output": True,
                    "text": True,
                    "timeout": 2.0,
                }
                if sys.platform == "win32":
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    kwargs["startupinfo"] = startupinfo

                proc = subprocess.run([str(executable), "--version"], **kwargs)
                out = (proc.stdout or proc.stderr).strip()
                if "Python" in out:
                    ver = out.split()[1]
                    self._version_cache[exe_str] = ver
                    return ver
            except Exception as exc:
                _LOGGER.debug("Failed probing python version for %s: %s", executable, exc)

        return ""

    def _probe_venv_dir(self, venv_dir: Path, name: str, env_type: EnvironmentType) -> PythonEnvironment | None:
        """Inspect a directory to see if it is a valid virtual environment."""
        if not venv_dir.is_dir():
            return None

        # Windows layout
        win_exe = venv_dir / "Scripts" / "python.exe"
        if win_exe.is_file():
            ver = self.probe_python_version(win_exe)
            return PythonEnvironment(
                name=name,
                env_type=env_type,
                executable=win_exe,
                version=ver,
                prefix=venv_dir,
            )

        # Unix layout
        unix_exe = venv_dir / "bin" / "python"
        if unix_exe.is_file():
            ver = self.probe_python_version(unix_exe)
            return PythonEnvironment(
                name=name,
                env_type=env_type,
                executable=unix_exe,
                version=ver,
                prefix=venv_dir,
            )

        return None

    def _is_uv_env(self, venv_dir: Path) -> bool:
        """Check if a virtual environment was created by uv."""
        cfg = venv_dir / "pyvenv.cfg"
        if cfg.is_file():
            try:
                content = cfg.read_text(encoding="utf-8", errors="ignore").lower()
                if "uv" in content:
                    return True
            except Exception:
                pass
        return False

    def _get_poetry_virtualenv_dirs(self) -> list[Path]:
        """Resolve standard cache directories hosting Poetry virtual environments."""
        dirs: list[Path] = []
        if sys.platform == "win32":
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata:
                dirs.append(Path(local_appdata) / "pypoetry" / "Cache" / "virtualenvs")
        else:
            home = Path.home()
            dirs.append(home / ".cache" / "pypoetry" / "virtualenvs")
        return dirs

    def _discover_conda_environments(self) -> list[PythonEnvironment]:
        """Discover environments managed by Conda / Mamba."""
        envs: list[PythonEnvironment] = []
        conda_env_txt = Path.home() / ".conda" / "environments.txt"
        try:
            if conda_env_txt.is_file():
                lines = conda_env_txt.read_text(encoding="utf-8", errors="ignore").splitlines()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    prefix = Path(line)
                    if prefix.is_dir():
                        exe = prefix / ("python.exe" if sys.platform == "win32" else "bin/python")
                        if not exe.is_file() and sys.platform == "win32":
                            exe = prefix / "Scripts" / "python.exe"
                        if exe.is_file():
                            ver = self.probe_python_version(exe)
                            envs.append(
                                PythonEnvironment(
                                    name=prefix.name,
                                    env_type=EnvironmentType.CONDA,
                                    executable=exe,
                                    version=ver,
                                    prefix=prefix,
                                    details=f"Conda environment at {prefix}",
                                )
                            )
        except Exception as exc:
            _LOGGER.debug("Error reading conda environments.txt: %s", exc)

        # Check CONDA_PREFIX
        conda_prefix = os.environ.get("CONDA_PREFIX")
        if conda_prefix:
            prefix = Path(conda_prefix)
            exe = prefix / ("python.exe" if sys.platform == "win32" else "bin/python")
            if exe.is_file():
                ver = self.probe_python_version(exe)
                envs.append(
                    PythonEnvironment(
                        name=f"{prefix.name} (Active Conda)",
                        env_type=EnvironmentType.CONDA,
                        executable=exe,
                        version=ver,
                        prefix=prefix,
                        details="Active Conda session environment",
                    )
                )

        return envs

    def _discover_system_interpreters(self) -> list[PythonEnvironment]:
        """Discover global system-level Python interpreters."""
        envs: list[PythonEnvironment] = []

        # Windows: probe py -0p
        if sys.platform == "win32":
            py_launcher = shutil.which("py")
            if py_launcher:
                try:
                    kwargs: dict[str, Any] = {
                        "capture_output": True,
                        "text": True,
                        "timeout": 2.0,
                        "creationflags": subprocess.CREATE_NO_WINDOW,
                    }
                    proc = subprocess.run(["py", "-0p"], **kwargs)
                    for line in proc.stdout.splitlines():
                        line = line.strip()
                        if line.startswith("-") and "*" in line or line.startswith("-"):
                            parts = line.split(maxsplit=1)
                            if len(parts) == 2:
                                tag, path_str = parts
                                path_str = path_str.strip().lstrip("*").strip()
                                exe = Path(path_str)
                                if exe.is_file():
                                    ver = self.probe_python_version(exe)
                                    envs.append(
                                        PythonEnvironment(
                                            name=f"System {tag.lstrip('-')}",
                                            env_type=EnvironmentType.SYSTEM,
                                            executable=exe,
                                            version=ver,
                                            details="Registered Windows Python launcher interpreter",
                                        )
                                    )
                except Exception as exc:
                    _LOGGER.debug("Windows py -0p query failed: %s", exc)

        # General PATH discovery
        for alias in ("python3", "python", "python3.12", "python3.11", "python3.10"):
            found = shutil.which(alias)
            if found:
                exe = Path(found)
                ver = self.probe_python_version(exe)
                envs.append(
                    PythonEnvironment(
                        name=f"PATH {alias}",
                        env_type=EnvironmentType.SYSTEM,
                        executable=exe,
                        version=ver,
                        details=f"System interpreter on PATH ({found})",
                    )
                )

        return envs


# -----------------------------------------------------------------------------
# UI Components: Status Bar Selector & Picker Dialog
# -----------------------------------------------------------------------------


class EnvironmentSelectorWidget(QWidget):
    """Interactive status bar button displaying the active interpreter."""

    clicked = Signal()

    def __init__(self, palette: ColorPalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette: ColorPalette = palette
        self._current_env: PythonEnvironment | None = None

        self._button = QPushButton(self)
        self._button.setFlat(True)
        self._button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._button.clicked.connect(self.clicked.emit)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._button)

        self._apply_style()
        self.set_environment(None)

    def set_palette(self, palette: ColorPalette) -> None:
        self._palette = palette
        self._apply_style()

    def set_environment(self, env: PythonEnvironment | None) -> None:
        self._current_env = env
        if env:
            ver_text = f"Python {env.version}" if env.version else "Python"
            text = f"🐍 {ver_text} ({env.name})"
            self._button.setText(text)
            self._button.setToolTip(
                f"Active Interpreter: {env.executable}\n"
                f"Type: {env.env_type.value.upper()}\n"
                f"Prefix: {env.prefix}\n"
                f"Click to switch environment"
            )
        else:
            self._button.setText("🐍 Select Python Environment")
            self._button.setToolTip("Click to select an active Python interpreter")

    def get_environment(self) -> PythonEnvironment | None:
        return self._current_env

    def _apply_style(self) -> None:
        self._button.setStyleSheet(
            f"QPushButton {{ color: {self._palette.text}; background-color: transparent; "
            f"border: 1px solid transparent; border-radius: 3px; padding: 2px 8px; font-weight: 500; }}"
            f"QPushButton:hover {{ background-color: {self._palette.selection}; border-color: {self._palette.border}; }}"
        )


class EnvironmentPickerDialog(QDialog):
    """Modal dialog allowing users to pick from detected interpreters or browse a custom path."""

    environment_selected = Signal(PythonEnvironment)

    def __init__(
        self,
        environments: Sequence[PythonEnvironment],
        current_env: PythonEnvironment | None,
        palette: ColorPalette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Python Interpreter")
        self.resize(650, 420)
        self._palette: ColorPalette = palette
        self._all_environments: list[PythonEnvironment] = list(environments)
        self._current_env: PythonEnvironment | None = current_env
        self._selected_env: PythonEnvironment | None = None

        self._filter_input = QLineEdit(self)
        self._filter_input.setPlaceholderText("Filter environments by name, type, or version...")
        self._filter_input.textChanged.connect(self._apply_filter)

        self._table = QTableWidget(0, 4, self)
        self._table.setHorizontalHeaderLabels(["Environment", "Type", "Version", "Executable Path"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.itemDoubleClicked.connect(self._on_table_double_clicked)

        browse_btn = QPushButton("📁 Browse Custom Interpreter...", self)
        browse_btn.clicked.connect(self._on_browse)

        select_btn = QPushButton("Select Environment", self)
        select_btn.setProperty("role", "primary")
        select_btn.clicked.connect(self._on_select)

        cancel_btn = QPushButton("Cancel", self)
        cancel_btn.clicked.connect(self.reject)

        btn_layout = QHBoxLayout()
        btn_layout.addWidget(browse_btn)
        btn_layout.addStretch(1)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(select_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(QLabel("Available Python Interpreters:"))
        layout.addWidget(self._filter_input)
        layout.addWidget(self._table, 1)
        layout.addLayout(btn_layout)

        self._render(self._all_environments)
        self._apply_theme()

    def get_selected_environment(self) -> PythonEnvironment | None:
        return self._selected_env

    def _render(self, envs: list[PythonEnvironment]) -> None:
        self._table.setRowCount(0)
        selected_row = -1
        for row, env in enumerate(envs):
            self._table.insertRow(row)

            prefix_icon = "● " if self._current_env and self._current_env.executable == env.executable else "  "
            name_item = QTableWidgetItem(f"{prefix_icon}{env.name}")
            type_item = QTableWidgetItem(env.env_type.value.upper())
            ver_item = QTableWidgetItem(env.version or "Unknown")
            path_item = QTableWidgetItem(str(env.executable))

            # Store the environment object on the row
            name_item.setData(Qt.ItemDataRole.UserRole, env)

            if self._current_env and self._current_env.executable == env.executable:
                selected_row = row
                name_item.setForeground(QColor(self._palette.green))
                type_item.setForeground(QColor(self._palette.green))

            self._table.setItem(row, 0, name_item)
            self._table.setItem(row, 1, type_item)
            self._table.setItem(row, 2, ver_item)
            self._table.setItem(row, 3, path_item)

        if selected_row >= 0:
            self._table.selectRow(selected_row)
        elif self._table.rowCount() > 0:
            self._table.selectRow(0)

    def _apply_filter(self, text: str) -> None:
        query = text.strip().lower()
        if not query:
            self._render(self._all_environments)
            return

        filtered = [
            env for env in self._all_environments
            if query in env.name.lower()
            or query in env.env_type.value.lower()
            or query in env.version.lower()
            or query in str(env.executable).lower()
        ]
        self._render(filtered)

    @Slot()
    def _on_select(self) -> None:
        selected_rows = self._table.selectionModel().selectedRows()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        item = self._table.item(row, 0)
        if item:
            env = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(env, PythonEnvironment):
                self._selected_env = env
                self.environment_selected.emit(env)
                self.accept()

    @Slot(QTableWidgetItem)
    def _on_table_double_clicked(self, item: QTableWidgetItem) -> None:
        self._on_select()

    @Slot()
    def _on_browse(self) -> None:
        filter_spec = "Python Executable (python.exe python);;All Files (*.*)" if sys.platform == "win32" else "Python Executable (python*);;All Files (*)"
        chosen, _ = QFileDialog.getOpenFileName(self, "Select Python Executable", str(Path.home()), filter_spec)
        if chosen:
            chosen_path = Path(chosen)
            if chosen_path.is_file():
                detector = EnvironmentDetector()
                ver = detector.probe_python_version(chosen_path)
                custom_env = PythonEnvironment(
                    name=chosen_path.name,
                    env_type=EnvironmentType.CUSTOM,
                    executable=chosen_path,
                    version=ver,
                    details="Custom user-selected interpreter",
                )
                self._selected_env = custom_env
                self.environment_selected.emit(custom_env)
                self.accept()

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color: {self._palette.background}; color: {self._palette.text}; }}"
            f"QLabel {{ color: {self._palette.text}; font-weight: bold; }}"
            f"QLineEdit {{ background-color: {self._palette.panel}; color: {self._palette.text}; "
            f"border: 1px solid {self._palette.border}; border-radius: 4px; padding: 6px; }}"
            f"QTableWidget {{ background-color: {self._palette.panel}; color: {self._palette.text}; "
            f"border: 1px solid {self._palette.border}; gridline-color: {self._palette.border}; "
            f"selection-background-color: {self._palette.selection}; }}"
            f"QHeaderView::section {{ background-color: {self._palette.background}; color: {self._palette.text}; "
            f"border: 1px solid {self._palette.border}; padding: 4px; font-weight: bold; }}"
            f"QPushButton {{ background-color: {self._palette.panel}; color: {self._palette.text}; "
            f"border: 1px solid {self._palette.border}; border-radius: 4px; padding: 6px 14px; }}"
            f"QPushButton:hover {{ background-color: {self._palette.selection}; }}"
            f"QPushButton[role='primary'] {{ background-color: {self._palette.blue}; color: #ffffff; font-weight: bold; }}"
        )
