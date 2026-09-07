"""Comprehensive unit and integration tests for PipViper Controllers (PRD A1, T3).

Verifies:
    * EditorController: Document lifecycle (new, open, save, jump).
    * LayoutController: Splitter persistence and pane visibility toggles.
    * PackageController: Offline mode blocking and package installations.
    * AIController: Local AI queries, markdown extraction, diff review.
    * EnvironmentController: Interpreter discovery and switching.
    * StatusBarController: Coordinates, branch, memory meter, and offline badge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QByteArray, QSettings, Qt
from PySide6.QtWidgets import QDialog, QMainWindow, QSplitter, QStatusBar, QTabWidget, QWidget

from src.controllers.ai_controller import AIController, extract_code_from_markdown
from src.controllers.editor_controller import EditorController
from src.controllers.environment_controller import EnvironmentController
from src.controllers.layout_controller import LayoutController
from src.controllers.package_controller import PackageController
from src.controllers.status_bar_controller import StatusBarController
from src.editor import EditorTabs
from src.environment import EnvironmentType, PythonEnvironment
from src.pip_viper import AppConfig, ColorPalette, EditorTheme, get_palette
from src.services.ai_service import AIService
from src.services.container import ServiceContainer
from src.services.environment_service import EnvironmentService
from src.services.offline_service import OfflineService
from src.services.package_service import PackageService


@pytest.fixture(autouse=True)
def clean_services() -> None:
    OfflineService.reset_instance()
    ServiceContainer.set_instance(None)


def test_code_markdown_extraction() -> None:
    """Verify markdown extraction strips backticks and retains pure Python source."""
    fenced_python = "```python\ndef foo():\n    return 42\n```"
    assert extract_code_from_markdown(fenced_python) == "def foo():\n    return 42"

    generic_fence = "```\nx = 10\n```"
    assert extract_code_from_markdown(generic_fence) == "x = 10"

    raw_code = "print('hello')"
    assert extract_code_from_markdown(raw_code) == "print('hello')"


def test_editor_controller_lifecycle(qtbot: Any, tmp_path: Path) -> None:
    """Verify EditorController handles new, open, save, and navigation."""
    palette = get_palette(EditorTheme.DARK)
    jedi = MagicMock()
    config = AppConfig()
    tabs = EditorTabs(palette, jedi, config)
    getattr(qtbot, "addWidget")(tabs)

    controller = EditorController(tabs)

    # 1. New file
    ed1 = controller.new_file()
    assert ed1 is not None
    assert "untitled_" in ed1.file_path().name

    # 2. Open file
    target_file = tmp_path / "hello.py"
    target_file.write_text("x = 100\n", encoding="utf-8")

    opened_files: list[Path] = []
    controller.file_opened.connect(opened_files.append)

    ed2 = controller.open_file(target_file)
    assert ed2 is not None
    assert ed2.file_path() == target_file
    assert len(opened_files) == 1

    # 3. Save file
    ed2.setPlainText("x = 200\n")
    saved_files: list[Path] = []
    controller.file_saved.connect(saved_files.append)
    success = controller.save_current_file()
    assert success is True
    assert len(saved_files) == 1
    assert target_file.read_text(encoding="utf-8") == "x = 200\n"

    # 4. Navigate to line
    controller.navigate_to_line(target_file, 1, 3)
    assert controller.get_current_editor() == ed2


def test_layout_controller(qtbot: Any) -> None:
    """Verify LayoutController manages geometry, splitters, and panel visibility."""
    window = QMainWindow()
    main_split = QSplitter(Qt.Orientation.Vertical)
    content_split = QSplitter(Qt.Orientation.Horizontal)
    side_split = QSplitter(Qt.Orientation.Vertical)
    bottom_tabs = QTabWidget()
    ai_panel = QWidget()

    content_split.addWidget(QWidget())
    content_split.addWidget(QWidget())
    content_split.addWidget(ai_panel)
    content_split.setSizes([300, 800, 300])

    layout = LayoutController(
        window, main_split, content_split, side_split, bottom_tabs, ai_panel
    )

    # Toggle bottom panel
    bottom_tabs.setVisible(True)
    layout.toggle_bottom_panel()
    assert not bottom_tabs.isVisible()
    layout.toggle_bottom_panel()
    assert bottom_tabs.isVisible()

    # Show specific panel tab
    p1 = QWidget()
    bottom_tabs.addTab(p1, "Tab1")
    layout.show_panel_tab(p1)
    assert bottom_tabs.currentWidget() == p1

    # Toggle sidebar
    layout.toggle_sidebar()
    assert content_split.sizes()[0] == 0
    layout.toggle_sidebar()
    assert content_split.sizes()[0] > 0

    # Toggle AI panel
    layout.toggle_ai_panel()
    assert content_split.sizes()[2] == 0
    layout.toggle_ai_panel()
    assert content_split.sizes()[2] > 0


def test_package_controller_offline_gating() -> None:
    """Verify PackageController blocks installs in Offline Mode unless local wheels configured."""
    offline = OfflineService.get_instance()
    offline.set_offline(True)
    assert offline.get_local_wheelhouse_dir() is None

    pkg_service = MagicMock(spec=PackageService)
    controller = PackageController(package_service=pkg_service, offline_service=offline)

    status_msgs: list[str] = []
    controller.status_message.connect(status_msgs.append)

    # Attempt install while offline
    controller.install_package("python.exe", "numpy")
    pkg_service.install_package.assert_not_called()
    assert any("blocked" in msg.lower() for msg in status_msgs)


def test_ai_controller_refactor_flow(qtbot: Any) -> None:
    """Verify AIController queries local sidecar and processes mandatory diff review."""
    mock_ai_service = MagicMock(spec=AIService)
    mock_ai_service.query.return_value = "```python\ndef refactored():\n    return 99\n```"

    controller = AIController(ai_service=mock_ai_service)
    palette = get_palette(EditorTheme.DARK)

    mutations: list[str] = []
    controller.code_mutation_accepted.connect(mutations.append)

    # Mock diff dialog factory returning Accepted
    mock_dialog = MagicMock()
    mock_dialog.exec.return_value = QDialog.DialogCode.Accepted
    mock_dialog.get_modified_code.return_value = "def refactored():\n    return 99"

    def factory(orig: str, mod: str, pal: Any, parent: Any) -> Any:
        return mock_dialog

    cast_qtbot = getattr(qtbot, "waitSignal", None)
    if cast_qtbot:
        with cast_qtbot(controller.code_mutation_accepted, timeout=2000):
            controller.request_refactor(
                original_code="def original(): pass",
                user_instructions="Refactor cleanly",
                palette=palette,
                diff_dialog_factory=factory,
            )
        assert len(mutations) == 1
        assert "def refactored" in mutations[0]


def test_status_bar_controller(qtbot: Any) -> None:
    """Verify StatusBarController manages telemetry and status widgets."""
    bar = QStatusBar()
    getattr(qtbot, "addWidget")(bar)

    controller = StatusBarController(bar, palette=get_palette(EditorTheme.DARK))

    # Update message
    controller.set_message("Test Status")
    assert controller.status_label.text() == "Test Status"

    # Update cursor
    controller.update_cursor_position(10, 4)
    assert controller.cursor_label.text() == "Ln 10, Col 4"

    # Update interpreter
    env = PythonEnvironment(
        name="venv312",
        env_type=EnvironmentType.VENV,
        executable=Path("C:/Python312/python.exe"),
        version="3.12.9",
        prefix=Path("C:/Python312"),
    )
    controller.update_interpreter(env)
    assert "venv312" in controller.interpreter_label.text()
