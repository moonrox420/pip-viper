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
# Debug Panel
# -----------------------------------------------------------------------------


class DebugPanel(QWidget):
    """Complete visual graphical debugging panel for PipViper.

    Features interactive stepping controls (Continue, Step Over, Step Into,
    Step Out, Stop, Restart), call stack navigation, expandable locals and
    globals variables inspection, breakpoint management, program console
    stream, and remote debugpy listener support.
    """

    start_requested = Signal()
    continue_requested = Signal()
    step_over_requested = Signal()
    step_into_requested = Signal()
    step_out_requested = Signal()
    stop_requested = Signal()
    restart_requested = Signal()
    frame_jump_requested = Signal(str, int)  # file_path, line_number
    frame_selected = Signal(int)  # frame level (0 = current/top)
    breakpoint_removed = Signal(str, int)  # file_path, line_number
    launch_requested = Signal(int, bool)  # port, wait_for_client (debugpy)

    def __init__(self, palette: ColorPalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._all_locals: list[dict[str, Any]] = []
        self._all_globals: list[dict[str, Any]] = []
        self._call_stack_frames: list[dict[str, Any]] = []
        self._is_active: bool = False
        self._is_paused: bool = False

        # --- Top Stepping Control Bar ---
        self._status_label = QLabel("Debug Session: Inactive", self)
        self._status_label.setStyleSheet(f"font-weight: bold; color: {palette.muted};")

        self._start_btn = QPushButton("▷ Start (F5)", self)
        self._start_btn.setProperty("role", "primary")
        self._start_btn.setToolTip("Start debugging active script (F5)")
        self._start_btn.clicked.connect(self._on_start_or_continue)

        self._step_over_btn = QPushButton("↷ Step Over (F10)", self)
        self._step_over_btn.setToolTip("Step Over to next statement in current frame (F10)")
        self._step_over_btn.setEnabled(False)
        self._step_over_btn.clicked.connect(self.step_over_requested)

        self._step_into_btn = QPushButton("⇊ Step Into (F11)", self)
        self._step_into_btn.setToolTip("Step Into function call (F11)")
        self._step_into_btn.setEnabled(False)
        self._step_into_btn.clicked.connect(self.step_into_requested)

        self._step_out_btn = QPushButton("⇈ Step Out (Shift+F11)", self)
        self._step_out_btn.setToolTip("Step Out of current function to caller (Shift+F11)")
        self._step_out_btn.setEnabled(False)
        self._step_out_btn.clicked.connect(self.step_out_requested)

        self._stop_btn = QPushButton("⏹ Stop (Shift+F5)", self)
        self._stop_btn.setToolTip("Terminate debugging session (Shift+F5)")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self.stop_requested)

        self._restart_btn = QPushButton("🔄 Restart", self)
        self._restart_btn.setToolTip("Restart the debugging session")
        self._restart_btn.setEnabled(False)
        self._restart_btn.clicked.connect(self.restart_requested)

        ctrl_bar = QHBoxLayout()
        ctrl_bar.setContentsMargins(0, 0, 0, 4)
        ctrl_bar.addWidget(self._start_btn)
        ctrl_bar.addWidget(self._step_over_btn)
        ctrl_bar.addWidget(self._step_into_btn)
        ctrl_bar.addWidget(self._step_out_btn)
        ctrl_bar.addWidget(self._restart_btn)
        ctrl_bar.addWidget(self._stop_btn)
        ctrl_bar.addSpacing(12)
        ctrl_bar.addWidget(self._status_label, 1)

        # --- Left Pane: Call Stack & Breakpoints ---
        left_splitter = QSplitter(Qt.Orientation.Vertical, self)

        # Call Stack Box
        stack_box = QGroupBox("📚 Call Stack", self)
        stack_layout = QVBoxLayout(stack_box)
        stack_layout.setContentsMargins(4, 4, 4, 4)
        self._call_stack_list = QListWidget(self)
        self._call_stack_list.setFont(QFont("Consolas", 9))
        self._call_stack_list.itemClicked.connect(self._on_stack_item_clicked)
        stack_layout.addWidget(self._call_stack_list)
        left_splitter.addWidget(stack_box)

        # Breakpoints Box
        bp_box = QGroupBox("🔴 Breakpoints", self)
        bp_layout = QVBoxLayout(bp_box)
        bp_layout.setContentsMargins(4, 4, 4, 4)
        self._breakpoints_table = QTableWidget(self)
        self._breakpoints_table.setColumnCount(3)
        self._breakpoints_table.setHorizontalHeaderLabels(["File", "Line", "Action"])
        self._breakpoints_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._breakpoints_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._breakpoints_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self._breakpoints_table.verticalHeader().setVisible(False)
        self._breakpoints_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        bp_layout.addWidget(self._breakpoints_table)
        left_splitter.addWidget(bp_box)

        left_splitter.setSizes([180, 140])

        # --- Right Pane: Variables Tree, Console, and Remote debugpy ---
        right_tabs = QTabWidget(self)

        # Tab 1: Variables Tree
        var_container = QWidget(self)
        var_layout = QVBoxLayout(var_container)
        var_layout.setContentsMargins(4, 4, 4, 4)

        var_filter_row = QHBoxLayout()
        self._var_filter_input = QLineEdit(self)
        self._var_filter_input.setPlaceholderText("Filter variables by symbol name...")
        self._var_filter_input.textChanged.connect(self._apply_var_filter)
        var_filter_row.addWidget(QLabel("🔍", self))
        var_filter_row.addWidget(self._var_filter_input)
        var_layout.addLayout(var_filter_row)

        self._variables_tree = QTreeWidget(self)
        self._variables_tree.setHeaderLabels(
            ["Symbol", "Type", "Size / Shape", "Memory", "Value Preview"]
        )
        self._variables_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        self._variables_tree.header().resizeSection(0, 160)
        self._variables_tree.header().resizeSection(1, 100)
        self._variables_tree.header().resizeSection(2, 100)
        self._variables_tree.header().resizeSection(3, 80)
        self._variables_tree.header().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        var_layout.addWidget(self._variables_tree)
        right_tabs.addTab(var_container, "🔍 Variables")

        # Tab 2: Console Log
        self._console = QPlainTextEdit(self)
        self._console.setReadOnly(True)
        self._console.setFont(QFont("Consolas", 10))
        right_tabs.addTab(self._console, "📜 Debug Console")

        # Tab 3: Remote debugpy
        remote_container = QWidget(self)
        remote_layout = QVBoxLayout(remote_container)
        remote_layout.setContentsMargins(8, 8, 8, 8)
        port_input = QLineEdit("5678", self)
        wait_cb = QCheckBox("Wait for client", self)
        wait_cb.setChecked(True)
        launch_btn = QPushButton("🚀 Launch debugpy Listener", self)
        launch_btn.setProperty("role", "primary")
        launch_btn.clicked.connect(lambda: self._launch(port_input, wait_cb))
        remote_row = QHBoxLayout()
        remote_row.addWidget(QLabel("Port:"))
        remote_row.addWidget(port_input)
        remote_row.addWidget(wait_cb)
        remote_row.addWidget(launch_btn)
        remote_row.addStretch(1)
        remote_layout.addLayout(remote_row)
        remote_layout.addStretch(1)
        right_tabs.addTab(remote_container, "🌐 Remote debugpy")

        # --- Center Horizontal Splitter ---
        center_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        center_splitter.addWidget(left_splitter)
        center_splitter.addWidget(right_tabs)
        center_splitter.setStretchFactor(0, 1)
        center_splitter.setStretchFactor(1, 2)
        center_splitter.setSizes([320, 580])

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.addLayout(ctrl_bar)
        main_layout.addWidget(center_splitter, 1)

        self._apply_colors()

    def set_palette(self, palette: ColorPalette) -> None:
        """Dynamically refresh console and panel colors on theme changes."""
        self._palette = palette
        self._apply_colors()

    def _apply_colors(self) -> None:
        self._console.setStyleSheet(
            f"background-color: {self._palette.background}; color: {self._palette.text};"
        )
        self._call_stack_list.setStyleSheet(
            f"background-color: {self._palette.background}; color: {self._palette.text};"
        )
        self._variables_tree.setStyleSheet(
            f"background-color: {self._palette.background}; color: {self._palette.text};"
        )
        self._breakpoints_table.setStyleSheet(
            f"background-color: {self._palette.background}; color: {self._palette.text};"
        )

    def _on_start_or_continue(self) -> None:
        if self._is_paused:
            self.continue_requested.emit()
        else:
            self.start_requested.emit()

    def set_session_state(
        self,
        state: str,
        file: str = "",
        line: int = 0,
        exception_desc: str | None = None,
    ) -> None:
        """Update toolbar buttons and status badge to reflect current debugger state."""
        state_lower = state.lower()
        if state_lower == "running":
            self._is_active = True
            self._is_paused = False
            fname = Path(file).name if file else "script"
            self._status_label.setText(f"Debug Session: RUNNING ({fname})")
            self._status_label.setStyleSheet(
                f"font-weight: bold; color: {self._palette.green};"
            )
            self._start_btn.setText("▷ Continue (F5)")
            self._start_btn.setEnabled(False)
            self._step_over_btn.setEnabled(False)
            self._step_into_btn.setEnabled(False)
            self._step_out_btn.setEnabled(False)
            self._stop_btn.setEnabled(True)
            self._restart_btn.setEnabled(True)

        elif state_lower == "paused":
            self._is_active = True
            self._is_paused = True
            fname = Path(file).name if file else "script"
            if exception_desc:
                self._status_label.setText(
                    f"Debug Session: EXCEPTION at ln {line} ({exception_desc[:40]})"
                )
                self._status_label.setStyleSheet(
                    f"font-weight: bold; color: {self._palette.red};"
                )
            else:
                self._status_label.setText(
                    f"Debug Session: PAUSED at line {line} in {fname}"
                )
                self._status_label.setStyleSheet(
                    f"font-weight: bold; color: {self._palette.yellow};"
                )
            self._start_btn.setText("▷ Continue (F5)")
            self._start_btn.setEnabled(True)
            self._step_over_btn.setEnabled(True)
            self._step_into_btn.setEnabled(True)
            self._step_out_btn.setEnabled(True)
            self._stop_btn.setEnabled(True)
            self._restart_btn.setEnabled(True)

        else:  # inactive or terminated
            self._is_active = False
            self._is_paused = False
            self._status_label.setText("Debug Session: Inactive")
            self._status_label.setStyleSheet(
                f"font-weight: bold; color: {self._palette.muted};"
            )
            self._start_btn.setText("▷ Start (F5)")
            self._start_btn.setEnabled(True)
            self._step_over_btn.setEnabled(False)
            self._step_into_btn.setEnabled(False)
            self._step_out_btn.setEnabled(False)
            self._stop_btn.setEnabled(False)
            self._restart_btn.setEnabled(False)

    def set_session_active(self, active: bool, port: int | None = None) -> None:
        """Backward-compatible helper for remote debugpy sessions."""
        self._is_active = active
        status = (
            f"Debug Session: {'ACTIVE (port ' + str(port) + ')' if active else 'Inactive'}"
        )
        self._status_label.setText(status)

    def update_call_stack(self, frames: list[dict[str, Any]]) -> None:
        """Render active call stack frames into the list widget."""
        self._call_stack_frames = frames
        self._call_stack_list.clear()

        for idx, f in enumerate(frames):
            level = f.get("level", idx)
            func = f.get("function", "?")
            fname = Path(f.get("file", "")).name
            line = f.get("line", 0)
            code_line = f.get("code_line", "")
            preview = f" #{level}  {func}() — {fname}:{line}"
            if code_line:
                preview += f"   [{code_line[:40]}]"
            item = QListWidgetItem(preview)
            item.setData(Qt.ItemDataRole.UserRole, f)
            self._call_stack_list.addItem(item)

        if self._call_stack_list.count() > 0:
            self._call_stack_list.setCurrentRow(0)

    def _on_stack_item_clicked(self, item: QListWidgetItem) -> None:
        frame_data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(frame_data, dict):
            file_path = frame_data.get("file", "")
            line = frame_data.get("line", 0)
            level = frame_data.get("level", 0)
            if file_path and line:
                self.frame_jump_requested.emit(file_path, line)
            self.frame_selected.emit(level)

    def update_variables(
        self,
        locals_data: list[dict[str, Any]],
        globals_data: list[dict[str, Any]],
    ) -> None:
        """Populate the Locals and Globals sections in the Variables Tree."""
        self._all_locals = locals_data
        self._all_globals = globals_data
        self._apply_var_filter(self._var_filter_input.text())

    def _apply_var_filter(self, filter_text: str) -> None:
        query = filter_text.strip().lower()
        self._variables_tree.clear()

        def _make_section(title: str, items: list[dict[str, Any]], expand: bool) -> None:
            filtered = [
                it for it in items if not query or query in it.get("name", "").lower()
            ]
            root = QTreeWidgetItem([f"{title} ({len(filtered)})", "", "", "", ""])
            root.setFont(0, QFont("Segoe UI", 9, QFont.Weight.Bold))
            root.setForeground(0, _qcolor(self._palette.purple))

            for var in filtered:
                name = var.get("name", "")
                tname = var.get("type_name", "object")
                srepr = var.get("size_repr", "-")
                mem = f"{var.get('memory_bytes', 0)} B"
                prev = var.get("value_preview", "")

                child = QTreeWidgetItem([name, tname, srepr, mem, prev])

                # Colorize types
                if tname in ("int", "float", "complex", "bool"):
                    child.setForeground(1, _qcolor(self._palette.number))
                elif tname in ("str", "bytes"):
                    child.setForeground(1, _qcolor(self._palette.string))
                elif tname in ("list", "dict", "tuple", "set"):
                    child.setForeground(1, _qcolor(self._palette.purple))
                elif "function" in tname or "method" in tname:
                    child.setForeground(1, _qcolor(self._palette.function_name))

                root.addChild(child)

            self._variables_tree.addTopLevelItem(root)
            if expand or query:
                root.setExpanded(True)

        _make_section("🔹 Locals", self._all_locals, expand=True)
        _make_section("🌐 Globals", self._all_globals, expand=bool(query))

    def update_breakpoints(self, bps_dict: dict[Path, set[int]]) -> None:
        """Render active breakpoints in the table with removal controls."""
        total_rows = sum(len(lines) for lines in bps_dict.values())
        self._breakpoints_table.setRowCount(total_rows)
        row = 0

        for file_path, lines in sorted(bps_dict.items(), key=lambda p: str(p[0])):
            for line in sorted(lines):
                file_item = QTableWidgetItem(file_path.name)
                file_item.setToolTip(str(file_path))
                line_item = QTableWidgetItem(f"Line {line}")
                line_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                del_btn = QPushButton("✕ Remove", self)
                del_btn.setMaximumWidth(70)
                del_btn.clicked.connect(
                    lambda _checked=False, fp=str(file_path), ln=line: self.breakpoint_removed.emit(
                        fp, ln
                    )
                )

                self._breakpoints_table.setItem(row, 0, file_item)
                self._breakpoints_table.setItem(row, 1, line_item)
                self._breakpoints_table.setCellWidget(row, 2, del_btn)
                row += 1

    def _launch(self, port_input: QLineEdit, wait_cb: QCheckBox) -> None:
        try:
            port = int(port_input.text().strip())
        except Exception:
            port = 5678
        self.launch_requested.emit(port, wait_cb.isChecked())

    def clear_console(self) -> None:
        self._console.clear()

    def log_message(self, msg: str) -> None:
        self._console.appendPlainText(msg.rstrip())


