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
# Log Panel
# -----------------------------------------------------------------------------


class LogWidget(QPlainTextEdit):
    """View tool for auditing system log file statements during operations."""

    LEVEL_COLORS: ClassVar[dict[int, str]] = {
        logging.DEBUG: "#586e75",
        logging.INFO: "#3fb950",
        logging.WARNING: "#d29922",
        logging.ERROR: "#ff7b72",
        logging.CRITICAL: "#ff7b72",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Consolas", 10))
        self.setMaximumBlockCount(10000)
        self._log_path: Path | None = None

    def load_file(self, log_path: Path) -> None:
        """Read system logs into text canvas."""
        self._log_path = log_path
        if not log_path.exists():
            self.setPlainText(f"(no log file yet at {log_path})")
            return
        try:
            self.setPlainText(log_path.read_text(encoding="utf-8", errors="replace"))
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.setTextCursor(cursor)
        except OSError as exception:
            self.setPlainText(f"(failed to read log: {exception})")

    def reload(self) -> None:
        """Reload the currently tracked log file, if any."""
        if self._log_path is not None:
            self.load_file(self._log_path)


class LogPanel(QWidget):
    """Wraps LogWidget with a small toolbar for refreshing and clearing the view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._log_widget = LogWidget(self)

        refresh_button = QPushButton("🔄 Refresh", self)
        refresh_button.setToolTip("Reload the log file from disk.")
        refresh_button.clicked.connect(self._log_widget.reload)
        clear_view_button = QPushButton("🧹 Clear View", self)
        clear_view_button.setToolTip(
            "Clear this view only; the log file on disk is untouched."
        )
        clear_view_button.clicked.connect(self._log_widget.clear)

        toolbar_layout = QHBoxLayout()
        toolbar_layout.addWidget(_section_label("SYSTEM LOG", self))
        toolbar_layout.addStretch(1)
        toolbar_layout.addWidget(refresh_button)
        toolbar_layout.addWidget(clear_view_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(toolbar_layout)
        layout.addWidget(self._log_widget)

    def load_file(self, log_path: Path) -> None:
        """Read system logs into the wrapped text canvas."""
        self._log_widget.load_file(log_path)


