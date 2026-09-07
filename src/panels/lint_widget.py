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
# Lint Panel
# -----------------------------------------------------------------------------


class LintWidget(QWidget):
    """Tabular layout for static code quality check outcomes."""

    run_requested = Signal()
    issue_activated = Signal(int, int)  # line, column (1-based, 0-based respectively)

    def __init__(self, palette: ColorPalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette: ColorPalette = palette
        self._issues_by_row: dict[int, LintIssue] = {}

        self._summary_label = QLabel("No lint results yet.", self)
        self._summary_label.setStyleSheet(f"color: {palette.muted};")

        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(
            ["Line", "Col", "Code", "Tool", "Message"]
        )
        self._table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setColumnWidth(0, 60)
        self._table.setColumnWidth(1, 50)
        self._table.setColumnWidth(2, 80)
        self._table.setColumnWidth(3, 80)
        self._table.itemDoubleClicked.connect(self._on_item_double_clicked)

        run_button = QPushButton("🔍 Run Linters", self)
        run_button.setProperty("role", "primary")
        run_button.clicked.connect(self.run_requested)
        clear_button = QPushButton("🧹 Clear", self)
        clear_button.clicked.connect(self._clear)

        toolbar_layout = QHBoxLayout()
        toolbar_layout.addWidget(run_button)
        toolbar_layout.addWidget(clear_button)
        toolbar_layout.addStretch(1)
        toolbar_layout.addWidget(self._summary_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(toolbar_layout)
        layout.addWidget(self._table)

    def set_palette(self, palette: ColorPalette) -> None:
        """Update active colors across components."""
        self._palette = palette

    def populate(self, issues: Sequence[LintIssue]) -> None:
        """Fill table workspace with structured analysis findings."""
        self._issues_by_row.clear()
        self._table.setRowCount(len(issues))
        error_count = sum(1 for issue in issues if issue.severity == LintSeverity.ERROR)
        warning_count = sum(
            1 for issue in issues if issue.severity == LintSeverity.WARNING
        )
        other_count = len(issues) - error_count - warning_count

        for row_index, lint_issue in enumerate(issues):
            self._issues_by_row[row_index] = lint_issue
            table_items = [
                QTableWidgetItem(str(lint_issue.line)),
                QTableWidgetItem(str(lint_issue.column)),
                QTableWidgetItem(lint_issue.code),
                QTableWidgetItem(lint_issue.tool.value),
                QTableWidgetItem(lint_issue.message),
            ]
            color = _severity_color(self._palette, lint_issue.severity.value)
            for column_index, item in enumerate(table_items):
                item.setForeground(_qcolor(color))
                self._table.setItem(row_index, column_index, item)

        if issues:
            self._summary_label.setText(
                f"{len(issues)} issue(s): {error_count} error(s), "
                f"{warning_count} warning(s), {other_count} other."
            )
        else:
            self._summary_label.setText("No issues detected.")

    def _clear(self) -> None:
        self._table.setRowCount(0)
        self._issues_by_row.clear()
        self._summary_label.setText("No lint results yet.")

    @Slot(QTableWidgetItem)
    def _on_item_double_clicked(self, item: QTableWidgetItem) -> None:
        issue = self._issues_by_row.get(item.row())
        if issue is not None:
            self.issue_activated.emit(issue.line, issue.column)


