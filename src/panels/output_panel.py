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
# Output Panel
# -----------------------------------------------------------------------------


class OutputPanel(QWidget):
    """Aggregated stream view for standard outputs and error channels."""

    def __init__(self, palette: ColorPalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette: ColorPalette = palette
        self._stdout_view: QPlainTextEdit = QPlainTextEdit(self)
        self._stderr_view: QPlainTextEdit = QPlainTextEdit(self)

        font = QFont("Consolas", 11)
        for view in (self._stdout_view, self._stderr_view):
            view.setReadOnly(True)
            view.setFont(font)
            view.setMaximumBlockCount(5000)

        self._apply_colors()

        clear_button = QPushButton("🧹 Clear", self)
        clear_button.clicked.connect(self.clear)
        toolbar_layout = QHBoxLayout()
        toolbar_layout.addWidget(_section_label("PROCESS OUTPUT", self))
        toolbar_layout.addStretch(1)
        toolbar_layout.addWidget(clear_button)

        stdout_box = QGroupBox("▶ stdout", self)
        stdout_layout = QVBoxLayout(stdout_box)
        stdout_layout.addWidget(self._stdout_view)

        stderr_box = QGroupBox("⨯ stderr", self)
        stderr_layout = QVBoxLayout(stderr_box)
        stderr_layout.addWidget(self._stderr_view)

        panes_layout = QHBoxLayout()
        panes_layout.addWidget(stdout_box, 1)
        panes_layout.addWidget(stderr_box, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(toolbar_layout)
        layout.addLayout(panes_layout)

    def set_palette(self, palette: ColorPalette) -> None:
        """Dynamically refresh visual stream colors on theme changes."""
        self._palette = palette
        self._apply_colors()

    def _apply_colors(self) -> None:
        self._stdout_view.setStyleSheet(
            f"color: {self._palette.green}; background: {self._palette.background};"
        )
        self._stderr_view.setStyleSheet(
            f"color: {self._palette.red}; background: {self._palette.background};"
        )

    def append_stdout(self, text: str) -> None:
        """Append standard output logs."""
        self._stdout_view.appendPlainText(text.rstrip("\n"))

    def append_stderr(self, text: str) -> None:
        """Append standard error logs."""
        self._stderr_view.appendPlainText(text.rstrip("\n"))

    def clear(self) -> None:
        """Clear both standard output and standard error text canvases."""
        self._stdout_view.clear()
        self._stderr_view.clear()


