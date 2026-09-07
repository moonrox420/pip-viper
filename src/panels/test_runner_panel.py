"""Subpanel module extracted from src.panels."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, ClassVar, Optional, Sequence, cast

from pydantic import BaseModel, ConfigDict, Field
from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QObject,
    QPoint,
    QProcess,
    QProcessEnvironment,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..vcs import (
    GitBranch,
    GitCommit,
    GitFileStatus,
    GitService,
    GitStatusEntry,
)
from ..code_tools import CodeIssue
from ..internals import (
    AstInspectionResult,
    AstNodeInfo,
    DisassemblyInstruction,
    DisassemblyResult,
    MemoryHotspot,
    ProfileRecord,
    ProfileResult,
    ScopeInfo,
    ScopeSymbol,
    SymtableResult,
)
from ..testing import TestItem, TestStatus, TestSuiteSummary
from ..dependencies import (
    DependencyGraph,
    DependencyGraphView,
    DependencyNode,
    DependencyScanner,
)
from ..profiler_visualizer import FlameGraphWidget, CallHierarchyView
from ..memory_tracker import MemoryTrackerWidget
from .. import (
    ColorPalette,
    FileOperationError,
    LintIssue,
    LintSeverity,
    PipPackage,
    ProcessTimeoutError,
    run_in_thread,
)
from .base import (
    _LOGGER,
    _SEVERITY_COLOR_KEYS,
    _severity_color,
    _section_label,
    _qcolor,
)

# -----------------------------------------------------------------------------
# Test Runner Panel
# -----------------------------------------------------------------------------


class TestRunnerPanel(QWidget):
    """Interactive visual Pytest test runner workstation.

    Delivers hierarchical test discovery tree, pass/fail status badges,
    execution durations, formatted failure tracebacks, real-time search filtering,
    and targeted one-click test execution.
    """

    __test__: bool = False

    run_all_requested = Signal()
    run_failed_requested = Signal()
    run_file_requested = Signal(object)  # Path
    run_test_requested = Signal(str)  # node_id
    stop_requested = Signal()
    refresh_requested = Signal()
    jump_to_source_requested = Signal(object, int)  # Path, line_number

    def __init__(self, palette: ColorPalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._all_items: list[TestItem] = []
        self._items_by_node_id: dict[str, TestItem] = {}
        self._tree_items_by_node_id: dict[str, QTreeWidgetItem] = {}
        self._active_file_path: Optional[Path] = None
        self._active_selected_item: Optional[TestItem] = None

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)

        # --- Top Action Bar ---
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self._btn_run_all = QPushButton("▶ Run All", self)
        self._btn_run_all.setToolTip("Execute the entire Pytest test suite")
        self._btn_run_all.clicked.connect(self.run_all_requested.emit)

        self._btn_run_failed = QPushButton("🔴 Run Failed", self)
        self._btn_run_failed.setToolTip("Re-run only previously failed tests (--lf)")
        self._btn_run_failed.clicked.connect(self.run_failed_requested.emit)

        self._btn_run_file = QPushButton("📄 Run Active File", self)
        self._btn_run_file.setToolTip("Run tests declared in the active editor file")
        self._btn_run_file.clicked.connect(self._on_run_file_clicked)

        self._btn_refresh = QPushButton("🔄 Refresh", self)
        self._btn_refresh.setToolTip("Re-discover tests in project workspace")
        self._btn_refresh.clicked.connect(self.refresh_requested.emit)

        self._btn_stop = QPushButton("⏹ Stop", self)
        self._btn_stop.setToolTip("Stop active test execution")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self.stop_requested.emit)

        self._filter_input = QLineEdit(self)
        self._filter_input.setPlaceholderText("Filter tests...")
        self._filter_input.textChanged.connect(self._apply_filter)

        self._summary_label = QLabel("Total: 0 | 🟢 0 | 🔴 0 | ⚪ 0", self)
        self._summary_label.setStyleSheet(f"font-weight: bold; color: {palette.text};")

        toolbar.addWidget(self._btn_run_all)
        toolbar.addWidget(self._btn_run_failed)
        toolbar.addWidget(self._btn_run_file)
        toolbar.addWidget(self._btn_refresh)
        toolbar.addWidget(self._btn_stop)
        toolbar.addWidget(self._filter_input)
        toolbar.addStretch()
        toolbar.addWidget(self._summary_label)
        main_layout.addLayout(toolbar)

        # --- Splitter: Tree on left, Traceback on right ---
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Left: Test Hierarchy Tree
        tree_container = QWidget(splitter)
        tree_layout = QVBoxLayout(tree_container)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(4)

        self._tree = QTreeWidget(tree_container)
        self._tree.setHeaderLabels(["Test", "Status", "Duration"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        tree_layout.addWidget(self._tree)
        splitter.addWidget(tree_container)

        # Right: Traceback & Details Viewer
        detail_container = QWidget(splitter)
        detail_layout = QVBoxLayout(detail_container)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(4)

        detail_bar = QHBoxLayout()
        self._selected_test_label = QLabel("Select a test to inspect details", detail_container)
        self._selected_test_label.setStyleSheet(f"font-weight: bold; color: {palette.text};")
        self._btn_jump = QPushButton("Jump to Source ↗", detail_container)
        self._btn_jump.setEnabled(False)
        self._btn_jump.clicked.connect(self._on_jump_clicked)

        detail_bar.addWidget(self._selected_test_label)
        detail_bar.addStretch()
        detail_bar.addWidget(self._btn_jump)
        detail_layout.addLayout(detail_bar)

        self._traceback_edit = QPlainTextEdit(detail_container)
        self._traceback_edit.setReadOnly(True)
        tb_font = QFont("Cascadia Code", 10)
        tb_font.setStyleHint(QFont.StyleHint.Monospace)
        self._traceback_edit.setFont(tb_font)
        detail_layout.addWidget(self._traceback_edit)

        splitter.addWidget(detail_container)
        splitter.setSizes([450, 450])
        main_layout.addWidget(splitter)

        self.set_palette(palette)

    def set_active_file(self, file_path: Path | None) -> None:
        """Track the currently active editor file for targeted execution."""
        self._active_file_path = file_path
        if file_path is not None:
            self._btn_run_file.setText(f"📄 Run {file_path.name}")
            self._btn_run_file.setEnabled(True)
        else:
            self._btn_run_file.setText("📄 Run Active File")
            self._btn_run_file.setEnabled(False)

    def _on_run_file_clicked(self) -> None:
        if self._active_file_path is not None:
            self.run_file_requested.emit(self._active_file_path)

    def set_tests(self, items: list[TestItem]) -> None:
        """Populate the hierarchical test discovery tree."""
        self._all_items = list(items)
        self._items_by_node_id = {item.node_id: item for item in items}
        self._tree_items_by_node_id.clear()
        self._tree.clear()

        # Group items by file_path, then optional class_name
        by_file: dict[str, list[TestItem]] = {}
        for item in items:
            by_file.setdefault(item.file_path, []).append(item)

        for file_path, file_items in sorted(by_file.items()):
            file_item = QTreeWidgetItem(self._tree)
            file_item.setText(0, f"📁 {file_path} ({len(file_items)})")
            file_item.setText(1, "")
            file_item.setText(2, "")
            file_item.setExpanded(True)

            by_class: dict[Optional[str], list[TestItem]] = {}
            for item in file_items:
                by_class.setdefault(item.class_name, []).append(item)

            for class_name, class_items in sorted(
                by_class.items(), key=lambda pair: pair[0] or ""
            ):
                parent_tree_item = file_item
                if class_name:
                    class_item = QTreeWidgetItem(file_item)
                    class_item.setText(0, f"📦 class {class_name}")
                    class_item.setText(1, "")
                    class_item.setText(2, "")
                    class_item.setExpanded(True)
                    parent_tree_item = class_item

                for test_item in class_items:
                    leaf = QTreeWidgetItem(parent_tree_item)
                    leaf.setText(0, f"⚡ {test_item.test_name}")
                    leaf.setText(1, "⚪ Pending")
                    leaf.setText(2, "")
                    leaf.setData(0, Qt.ItemDataRole.UserRole, test_item)
                    self._tree_items_by_node_id[test_item.node_id] = leaf

        self._update_summary_counts()

    def update_test_status(
        self,
        node_id: str,
        status: str,
        duration_ms: float = 0.0,
        traceback: str = "",
    ) -> None:
        """Update test state, timing badge, and traceback view in the tree."""
        item = self._items_by_node_id.get(node_id)
        if item is not None:
            item.status = status
            item.duration_ms = duration_ms
            if traceback:
                item.traceback = traceback

        tree_item = self._tree_items_by_node_id.get(node_id)
        if tree_item is not None:
            # Status badge
            if status == TestStatus.PASSED:
                tree_item.setText(1, "🟢 Passed")
            elif status == TestStatus.FAILED:
                tree_item.setText(1, "🔴 Failed")
            elif status == TestStatus.RUNNING:
                tree_item.setText(1, "🟡 Running...")
            elif status == TestStatus.SKIPPED:
                tree_item.setText(1, "⚪ Skipped")
            elif status == TestStatus.ERROR:
                tree_item.setText(1, "⚠️ Error")
            else:
                tree_item.setText(1, "⚪ Pending")

            # Duration badge
            if duration_ms > 0:
                if duration_ms < 1000:
                    tree_item.setText(2, f"{int(duration_ms)}ms")
                else:
                    tree_item.setText(2, f"{duration_ms / 1000.0:.2f}s")

        # If currently focused on this test in the right pane, update details
        if self._active_selected_item and self._active_selected_item.node_id == node_id:
            self._update_details_pane(item or self._active_selected_item)

        self._update_summary_counts()

    def _update_summary_counts(self) -> None:
        """Recalculate summary metrics across all known tests."""
        total = len(self._all_items)
        passed = sum(1 for i in self._all_items if i.status == TestStatus.PASSED)
        failed = sum(
            1
            for i in self._all_items
            if i.status in (TestStatus.FAILED, TestStatus.ERROR)
        )
        pending = sum(
            1
            for i in self._all_items
            if i.status in (TestStatus.PENDING, TestStatus.RUNNING)
        )
        self._summary_label.setText(
            f"Total: {total} | 🟢 {passed} Passed | 🔴 {failed} Failed | ⚪ {pending} Pending"
        )

    def set_running(self, running: bool) -> None:
        """Toggle active execution state across toolbar action buttons."""
        self._btn_stop.setEnabled(running)
        self._btn_run_all.setEnabled(not running)
        self._btn_run_failed.setEnabled(not running)
        self._btn_run_file.setEnabled(not running and self._active_file_path is not None)
        self._btn_refresh.setEnabled(not running)

    def set_summary(self, summary: TestSuiteSummary) -> None:
        """Display finalized execution summary banner."""
        self._summary_label.setText(
            f"Total: {summary.total} | 🟢 {summary.passed} Passed | 🔴 {summary.failed} Failed | ⏱ {summary.duration_sec:.2f}s"
        )

    def _apply_filter(self, query: str) -> None:
        """Filter the test tree items in real time."""
        clean_q = query.strip().lower()
        if not clean_q:
            for i in range(self._tree.topLevelItemCount()):
                self._show_item_recursive(self._tree.topLevelItem(i))
            return

        for i in range(self._tree.topLevelItemCount()):
            top_item = self._tree.topLevelItem(i)
            self._filter_item_recursive(top_item, clean_q)

    def _filter_item_recursive(self, item: QTreeWidgetItem, query: str) -> bool:
        matches = query in item.text(0).lower()
        child_match = False
        for c in range(item.childCount()):
            if self._filter_item_recursive(item.child(c), query):
                child_match = True
        should_show = matches or child_match
        item.setHidden(not should_show)
        if should_show and child_match:
            item.setExpanded(True)
        return should_show

    def _show_item_recursive(self, item: QTreeWidgetItem) -> None:
        item.setHidden(False)
        for c in range(item.childCount()):
            self._show_item_recursive(item.child(c))

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        test_item = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(test_item, TestItem):
            self._active_selected_item = test_item
            self._update_details_pane(test_item)

    def _update_details_pane(self, test_item: TestItem) -> None:
        self._selected_test_label.setText(f"{test_item.node_id} [{test_item.status.upper()}]")
        self._btn_jump.setEnabled(test_item.line_number is not None)

        if test_item.traceback:
            self._traceback_edit.setPlainText(test_item.traceback)
        elif test_item.status == TestStatus.PASSED:
            dur = f"{test_item.duration_ms:.1f}ms" if test_item.duration_ms > 0 else "<1ms"
            self._traceback_edit.setPlainText(f"✓ Test passed cleanly ({dur}).\nNo errors or failure tracebacks.")
        elif test_item.status == TestStatus.RUNNING:
            self._traceback_edit.setPlainText("⚡ Test is actively executing...")
        else:
            self._traceback_edit.setPlainText("Test pending execution.")

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        test_item = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(test_item, TestItem):
            self.jump_to_source_requested.emit(
                Path(test_item.file_path), test_item.line_number or 1
            )

    def _on_jump_clicked(self) -> None:
        if self._active_selected_item is not None:
            self.jump_to_source_requested.emit(
                Path(self._active_selected_item.file_path),
                self._active_selected_item.line_number or 1,
            )

    def _show_context_menu(self, pos: QPoint) -> None:
        item = self._tree.itemAt(pos)
        if not item:
            return
        test_item = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(test_item, TestItem):
            return

        menu = QMenu(self)
        run_act = menu.addAction("▶ Run This Test")
        jump_act = menu.addAction("📄 Jump to Source")
        copy_act = menu.addAction("📋 Copy Node ID")

        action = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if action == run_act:
            self.run_test_requested.emit(test_item.node_id)
        elif action == jump_act:
            self.jump_to_source_requested.emit(
                Path(test_item.file_path), test_item.line_number or 1
            )
        elif action == copy_act:
            clipboard = QGuiApplication.clipboard()
            if clipboard:
                clipboard.setText(test_item.node_id)

    def set_palette(self, palette: ColorPalette) -> None:
        """Apply active color palette across test runner interface elements."""
        self._palette = palette
        css = (
            f"QWidget {{ background-color: {palette.background}; color: {palette.text}; }}"
            f"QTreeWidget {{ background-color: {palette.background}; color: {palette.text}; border: 1px solid {palette.selection}; border-radius: 4px; }}"
            f"QTreeWidget::item:selected {{ background-color: {palette.selection}; color: {palette.text}; }}"
            f"QPlainTextEdit {{ background-color: {palette.background}; color: {palette.text}; border: 1px solid {palette.selection}; border-radius: 4px; }}"
            f"QPushButton {{ background-color: {palette.panel}; color: {palette.text}; border: 1px solid {palette.border}; padding: 4px 8px; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: {palette.selection}; }}"
            f"QPushButton:disabled {{ color: {palette.muted}; border-color: {palette.panel}; }}"
            f"QLineEdit {{ background-color: {palette.panel}; color: {palette.text}; border: 1px solid {palette.border}; padding: 3px 6px; border-radius: 4px; }}"
        )
        self.setStyleSheet(css)


