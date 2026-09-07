"""Testing Controller for PipViper IDE.

Orchestrates asynchronous test discovery, test suite execution, gutter test triggers,
and source navigation on failure.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QWidget

from ..editor import EditorTabs
from ..panels.test_runner_panel import TestRunnerPanel
from ..pip_viper import run_in_thread
from ..testing import PytestEngine, TestItem, TestStatus, TestSuiteSummary

_LOGGER = logging.getLogger("src.controllers.testing")


class TestingController(QObject):
    """Controller orchestrating test execution, discovery, and result reporting."""

    status_message = Signal(str)

    def __init__(
        self,
        test_panel: TestRunnerPanel,
        pytest_engine: PytestEngine,
        editor_tabs: EditorTabs,
        runtime_python_provider: Callable[[], str],
        project_root_provider: Callable[[], Path],
        open_file_callback: Optional[Callable[[Path], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._panel = test_panel
        self._engine = pytest_engine
        self._tabs = editor_tabs
        self._get_python = runtime_python_provider
        self._get_root = project_root_provider
        self._open_file = open_file_callback

        self._wire_signals()

    def _wire_signals(self) -> None:
        # Connect visual test panel user actions
        self._panel.run_all_requested.connect(self.run_all_tests)
        self._panel.run_failed_requested.connect(self.run_failed_tests)
        self._panel.refresh_requested.connect(self.refresh_test_discovery)
        self._panel.jump_to_source_requested.connect(self.jump_to_source_location)

        # Connect engine callbacks
        self._engine.test_started.connect(self._on_test_started)
        self._engine.test_finished.connect(self._on_test_finished)
        self._engine.run_finished.connect(self._on_test_run_finished)

    def refresh_test_discovery(self) -> None:
        """Asynchronously discover tests across the active project root."""
        project_root = self._get_root()
        py_exe = self._get_python()
        self.status_message.emit("Discovering tests...")

        def do_discover() -> list[TestItem]:
            return self._engine.discover_tests(project_root, py_exe)

        def on_discover_done(items: list[TestItem]) -> None:
            self._panel.set_tests(items)
            self.status_message.emit(f"Discovered {len(items)} test(s).")

        worker = run_in_thread(do_discover)
        worker.signals.result.connect(on_discover_done)

    @Slot()
    def run_all_tests(self) -> None:
        """Execute the entire test suite in the project workspace."""
        self._panel.set_running(True)
        project_root = self._get_root()
        py_exe = self._get_python()
        self.status_message.emit("Running full test suite...")
        self._engine.start_run(project_root, py_exe)

    @Slot()
    def run_failed_tests(self) -> None:
        """Re-run only previously failed tests."""
        self._panel.set_running(True)
        project_root = self._get_root()
        py_exe = self._get_python()
        self.status_message.emit("Running failed tests (--lf)...")
        self._engine.start_run(project_root, py_exe, failed_only=True)

    def run_file_tests(self, file_path: Path) -> None:
        """Run tests located inside a specific file."""
        self._panel.set_running(True)
        project_root = self._get_root()
        py_exe = self._get_python()
        self.status_message.emit(f"Running tests in {file_path.name}...")
        try:
            rel_path = str(file_path.relative_to(project_root)).replace("\\", "/")
        except ValueError:
            rel_path = str(file_path).replace("\\", "/")
        self._engine.start_run(project_root, py_exe, node_ids=[rel_path])

    def run_current_file_tests(self) -> None:
        """Run tests in the currently open editor tab."""
        editor = self._tabs.current_editor()
        if editor is not None:
            self.run_file_tests(editor.file_path())

    def run_single_test(self, node_id: str) -> None:
        """Execute a targeted test item by its Pytest node ID."""
        self._panel.set_running(True)
        project_root = self._get_root()
        py_exe = self._get_python()
        self.status_message.emit(f"Running test: {node_id}...")
        self._engine.start_run(project_root, py_exe, node_ids=[node_id])

    def on_gutter_test_run_requested(
        self, file_path: Path, test_name: str, _line: int
    ) -> None:
        """Handle user clicking a gutter play button in the code editor."""
        project_root = self._get_root()
        try:
            rel_path = str(file_path.relative_to(project_root)).replace("\\", "/")
        except ValueError:
            rel_path = str(file_path).replace("\\", "/")

        target_node = None
        for nid in self._panel._items_by_node_id:
            if nid.startswith(rel_path) and test_name in nid:
                target_node = nid
                break

        if target_node:
            self.run_single_test(target_node)
        else:
            self._panel.set_running(True)
            self._engine.start_run(
                project_root,
                self._get_python(),
                node_ids=[rel_path],
                extra_args=["-k", test_name],
            )

    @Slot(str)
    def _on_test_started(self, node_id: str) -> None:
        self._panel.update_test_status(node_id, TestStatus.RUNNING)

    @Slot(str, str, float, str)
    def _on_test_finished(
        self, node_id: str, status: str, duration_ms: float, traceback: str
    ) -> None:
        self._panel.update_test_status(node_id, status, duration_ms, traceback)

    @Slot(object)
    def _on_test_run_finished(self, summary: TestSuiteSummary) -> None:
        self._panel.set_running(False)
        self._panel.set_summary(summary)
        self.status_message.emit(
            f"Test run completed: {summary.passed} passed, {summary.failed} failed ({summary.duration_sec}s)"
        )

    def jump_to_source_location(self, file_path: Path, line: int) -> None:
        """Jump to the source file and line from a test item double-click."""
        project_root = self._get_root()
        full_path = project_root / file_path if not file_path.is_absolute() else file_path
        if full_path.is_file():
            if self._open_file:
                self._open_file(full_path)
            else:
                self._tabs.open_file(full_path)
            editor = self._tabs.current_editor()
            if editor:
                editor.jump_to_line(line, 0)
