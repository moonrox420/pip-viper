"""Tests for Phase 8: Virtual Environment Auto-Detection and Interpreter Switcher.

Covers:
    - EnvironmentType enum and PythonEnvironment model attributes/equality
    - EnvironmentDetector discovery of local .venv and system environments
    - pyvenv.cfg fast-path version probing
    - EnvironmentSelectorWidget status bar button rendering and click signal
    - EnvironmentPickerDialog list population, filter search, and selection
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from src import (
    ColorPalette,
    EditorTheme,
    EnvironmentDetector,
    EnvironmentPickerDialog,
    EnvironmentSelectorWidget,
    EnvironmentType,
    PythonEnvironment,
    get_palette,
)


@pytest.fixture
def palette() -> ColorPalette:
    return get_palette(EditorTheme.DARK)


def test_python_environment_properties(tmp_path: Path) -> None:
    exe = tmp_path / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")

    env = PythonEnvironment(
        name=".venv",
        env_type=EnvironmentType.VENV,
        executable=exe,
        version="3.12.9",
        prefix=tmp_path,
        is_active=True,
        details="Project local virtual environment",
    )

    assert env.name == ".venv"
    assert env.env_type == EnvironmentType.VENV
    assert env.version == "3.12.9"
    assert env.is_active is True
    assert "3.12.9" in env.display_label
    assert "venv" in env.display_label

    # Equality check
    env2 = PythonEnvironment(
        name="duplicate",
        env_type=EnvironmentType.VENV,
        executable=exe,
    )
    assert env == env2


def test_environment_detector_probe_version_fast_path(tmp_path: Path) -> None:
    venv_dir = tmp_path / ".venv"
    scripts = venv_dir / "Scripts"
    scripts.mkdir(parents=True)
    exe = scripts / "python.exe"
    exe.write_text("", encoding="utf-8")

    cfg = venv_dir / "pyvenv.cfg"
    cfg.write_text(
        "home = C:\\Python312\n"
        "version = 3.12.4\n"
        "executable = C:\\Python312\\python.exe\n",
        encoding="utf-8",
    )

    detector = EnvironmentDetector(project_root=tmp_path)
    version = detector.probe_python_version(exe)
    assert version == "3.12.4"


def test_environment_detector_discovers_local_venv(tmp_path: Path) -> None:
    # Set up mock .venv
    venv_dir = tmp_path / ".venv"
    scripts = venv_dir / "Scripts"
    scripts.mkdir(parents=True)
    exe = scripts / "python.exe"
    exe.write_text("", encoding="utf-8")

    cfg = venv_dir / "pyvenv.cfg"
    cfg.write_text("version = 3.12.9\n", encoding="utf-8")

    detector = EnvironmentDetector(project_root=tmp_path)
    envs = detector.discover_environments(tmp_path)

    assert len(envs) >= 1
    local_env = next((e for e in envs if e.name == ".venv"), None)
    assert local_env is not None
    assert local_env.version == "3.12.9"
    assert local_env.env_type == EnvironmentType.VENV


def test_environment_selector_widget(qtbot: Any, palette: ColorPalette, tmp_path: Path) -> None:
    widget = EnvironmentSelectorWidget(palette)
    getattr(qtbot, "addWidget")(widget)

    # Initially unassigned
    assert "Select" in widget._button.text()

    # Assign an environment
    exe = tmp_path / "python.exe"
    exe.write_text("", encoding="utf-8")
    env = PythonEnvironment(
        name=".venv",
        env_type=EnvironmentType.VENV,
        executable=exe,
        version="3.12.9",
    )
    widget.set_environment(env)
    assert "3.12.9" in widget._button.text()
    assert ".venv" in widget._button.text()
    assert widget.get_environment() == env

    # Test click signal
    with getattr(qtbot, "waitSignal")(widget.clicked, timeout=1000):
        widget._button.click()


def test_environment_picker_dialog_filter_and_selection(
    qtbot: Any, palette: ColorPalette, tmp_path: Path
) -> None:
    exe1 = tmp_path / "venv" / "python.exe"
    exe1.parent.mkdir(parents=True)
    exe1.write_text("", encoding="utf-8")
    env1 = PythonEnvironment("project_venv", EnvironmentType.VENV, exe1, "3.12.9")

    exe2 = tmp_path / "conda" / "python.exe"
    exe2.parent.mkdir(parents=True)
    exe2.write_text("", encoding="utf-8")
    env2 = PythonEnvironment("conda_env", EnvironmentType.CONDA, exe2, "3.11.5")

    dialog = EnvironmentPickerDialog([env1, env2], current_env=env1, palette=palette)
    getattr(qtbot, "addWidget")(dialog)

    assert dialog._table.rowCount() == 2

    # Test real-time filter search
    dialog._filter_input.setText("conda")
    assert dialog._table.rowCount() == 1
    assert "conda" in dialog._table.item(0, 0).text().lower()

    dialog._filter_input.clear()
    assert dialog._table.rowCount() == 2

    # Test selection
    dialog._table.selectRow(1)
    dialog._on_select()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.get_selected_environment() == env2
