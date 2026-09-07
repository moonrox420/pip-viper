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
# Code Tools Panel
# -----------------------------------------------------------------------------


class CodeToolsPanel(QWidget):
    """Control surface for the local Python auto-fixer, formatter, refactorer,
    and code generator. Optimized with a side-by-side horizontal flow to prevent
    layout squishing in constrained bottom docks.
    """

    check_syntax_requested = Signal()
    autofix_requested = Signal()
    format_requested = Signal()
    organize_imports_requested = Signal()
    remove_unused_requested = Signal()
    full_pipeline_requested = Signal()
    generate_docstrings_requested = Signal()
    generate_tests_requested = Signal()
    rename_requested = Signal(str, str)  # old_name, new_name
    issue_activated = Signal(int, int)   # line, column (for double-click jumps)

    def __init__(self, palette: ColorPalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette: ColorPalette = palette

        self._status_label = QLabel("Status: IDLE", self)
        self._status_label.setStyleSheet(f"color: {palette.muted}; font-weight: bold;")

        # --- Diagnose & Repair -------------------------------------------------
        diagnose_box = QGroupBox("🔎 Diagnose && Repair", self)
        check_button = QPushButton("🔎 Check Syntax", self)
        check_button.setToolTip("Validate the active file with Python's AST parser.")
        check_button.clicked.connect(self.check_syntax_requested)
        autofix_button = QPushButton("🩹 Auto-Fix", self)
        autofix_button.setProperty("role", "primary")
        autofix_button.setToolTip(
            "Repair common syntax errors (missing colons, unbalanced brackets,\n"
            "tab/space conflicts, unterminated strings, Python 2 print statements)."
        )
        autofix_button.clicked.connect(self.autofix_requested)
        diagnose_layout = QHBoxLayout(diagnose_box)
        diagnose_layout.addWidget(check_button)
        diagnose_layout.addWidget(autofix_button)

        # --- Format & Clean ------------------------------------------------------
        format_box = QGroupBox("🎨 Format && Clean", self)
        format_button = QPushButton("🎨 Format", self)
        format_button.clicked.connect(self.format_requested)
        organize_button = QPushButton("📚 Imports", self)
        organize_button.clicked.connect(self.organize_imports_requested)
        remove_unused_button = QPushButton("🧹 Unused", self)
        remove_unused_button.clicked.connect(self.remove_unused_requested)
        pipeline_button = QPushButton("✨ Pipeline", self)
        pipeline_button.setProperty("role", "primary")
        pipeline_button.setToolTip(
            "Auto-fix, remove unused code, organize imports, and format -- in order."
        )
        pipeline_button.clicked.connect(self.full_pipeline_requested)
        format_layout = QHBoxLayout(format_box)
        for button in (
            format_button,
            organize_button,
            remove_unused_button,
            pipeline_button,
        ):
            format_layout.addWidget(button)

        # --- Generate --------------------------------------------------------------
        generate_box = QGroupBox("🧬 Generate", self)
        docstrings_button = QPushButton("📝 Docstrings", self)
        docstrings_button.clicked.connect(self.generate_docstrings_requested)
        tests_button = QPushButton("🧪 Unit Tests", self)
        tests_button.setToolTip("Opens a new tab with a pytest skeleton for this file.")
        tests_button.clicked.connect(self.generate_tests_requested)
        generate_layout = QHBoxLayout(generate_box)
        generate_layout.addWidget(docstrings_button)
        generate_layout.addWidget(tests_button)

        # --- Rename Symbol -----------------------------------------------------
        rename_box = QGroupBox("🔤 Rename Symbol", self)
        self._rename_old_input = QLineEdit(self)
        self._rename_old_input.setPlaceholderText("current")
        self._rename_old_input.setFixedWidth(80)
        self._rename_new_input = QLineEdit(self)
        self._rename_new_input.setPlaceholderText("new")
        self._rename_new_input.setFixedWidth(80)
        rename_button = QPushButton("Rename", self)
        rename_button.clicked.connect(self._on_rename)
        rename_layout = QHBoxLayout(rename_box)
        rename_layout.addWidget(self._rename_old_input)
        rename_layout.addWidget(QLabel("→", self))
        rename_layout.addWidget(self._rename_new_input)
        rename_layout.addWidget(rename_button)

        self._action_buttons: list[QPushButton] = [
            check_button,
            autofix_button,
            format_button,
            organize_button,
            remove_unused_button,
            pipeline_button,
            docstrings_button,
            tests_button,
            rename_button,
        ]

        # --- Assemble side-by-side action row ---
        actions_layout = QHBoxLayout()
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(6)
        actions_layout.addWidget(diagnose_box)
        actions_layout.addWidget(format_box)
        actions_layout.addWidget(generate_box)
        actions_layout.addWidget(rename_box)

        # --- Results table + activity log ---
        self._results_table = QTableWidget(0, 3, self)
        self._results_table.setHorizontalHeaderLabels(["Line", "Col", "Message"])
        self._results_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self._results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._results_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._results_table.setColumnWidth(0, 60)
        self._results_table.setColumnWidth(1, 50)
        self._results_table.itemDoubleClicked.connect(self._on_item_double_clicked)

        self._activity_log = QPlainTextEdit(self)
        self._activity_log.setReadOnly(True)
        self._activity_log.setFont(QFont("Consolas", 10))
        self._activity_log.setPlaceholderText(
            "Applied fixes and pipeline results will be logged here..."
        )

        self._apply_colors()

        panes_layout = QHBoxLayout()
        panes_layout.addWidget(self._results_table, 1)
        panes_layout.addWidget(self._activity_log, 1)

        results_box = QGroupBox("📋 Diagnostics && Activity", self)
        results_layout = QVBoxLayout(results_box)
        results_layout.addLayout(panes_layout)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        layout.addWidget(self._status_label)
        layout.addLayout(actions_layout)
        layout.addWidget(results_box, 1)

    def set_palette(self, palette: ColorPalette) -> None:
        """Update active colors across input and text elements dynamically."""
        self._palette = palette
        self._apply_colors()

    def _apply_colors(self) -> None:
        self._activity_log.setStyleSheet(
            f"background-color: {self._palette.background}; color: {self._palette.text};"
        )

    def log(self, text: str) -> None:
        """Append a line to the activity log."""
        self._activity_log.appendPlainText(text)

    def set_busy(self, is_busy: bool) -> None:
        """Reflect whether a background code-tools operation is running."""
        for button in self._action_buttons:
            button.setEnabled(not is_busy)
        if is_busy:
            self._status_label.setText("Status: PROCESSING...")
            self._status_label.setStyleSheet(
                f"color: {self._palette.yellow}; font-weight: bold;"
            )
        else:
            self._status_label.setText("Status: IDLE")
            self._status_label.setStyleSheet(
                f"color: {self._palette.muted}; font-weight: bold;"
            )

    def populate_issues(self, issues: Sequence[CodeIssue]) -> None:
        """Fill the diagnostics table with syntax-checker findings."""
        self._results_table.setRowCount(len(issues))
        for row_index, issue in enumerate(issues):
            line_item = QTableWidgetItem(str(issue.line))
            column_item = QTableWidgetItem(str(issue.column))
            message_item = QTableWidgetItem(issue.message)
            color = _severity_color(self._palette, issue.severity.value)
            for item in (line_item, column_item, message_item):
                item.setForeground(_qcolor(color))
            self._results_table.setItem(row_index, 0, line_item)
            self._results_table.setItem(row_index, 1, column_item)
            self._results_table.setItem(row_index, 2, message_item)
        if not issues:
            self._results_table.setRowCount(1)
            ok_item = QTableWidgetItem("✓ No syntax issues detected.")
            ok_item.setForeground(_qcolor(self._palette.green))
            self._results_table.setItem(0, 0, QTableWidgetItem(""))
            self._results_table.setItem(0, 1, QTableWidgetItem(""))
            self._results_table.setItem(0, 2, ok_item)

    def clear_issues(self) -> None:
        """Clear the diagnostics table."""
        self._results_table.setRowCount(0)

    @Slot()
    def _on_rename(self) -> None:
        old_name = self._rename_old_input.text().strip()
        new_name = self._rename_new_input.text().strip()
        if not old_name or not new_name:
            self.log("Rename requires both a current name and a new name.")
            return
        self.rename_requested.emit(old_name, new_name)

    @Slot(QTableWidgetItem)
    def _on_item_double_clicked(self, item: QTableWidgetItem) -> None:
        """Emit the line and column details to trigger an editor code jump."""
        row_index = item.row()
        line_item = self._results_table.item(row_index, 0)
        col_item = self._results_table.item(row_index, 1)
        if line_item and col_item:
            try:
                line = int(line_item.text())
                column = int(col_item.text())
                self.issue_activated.emit(line, column)
            except ValueError:
                pass


