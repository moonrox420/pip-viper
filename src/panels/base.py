"""Interactive utility panels positioned at the bottom of the workspace.

This module delivers bottom-dock panels including:
    * OutputPanel: Separate stdout and stderr monitoring.
    * ReplPanel: Isolated local python subshell with terminal redirection.
    * CodeToolsPanel: Local syntax auto-fixer, formatter, refactorer, and
    code generator control surface.
    * AiPanel: Offline privacy-first AI assistant and refactoring control.
    * LintWidget: Static analysis issue layout.
    * GitWidget: Multi-threaded local version control dispatcher.
    * PackageManagerWidget: Local environment package discovery.
    * LogPanel: Active process logging inspector with refresh/clear controls.
"""

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

_LOGGER: logging.Logger = logging.getLogger("src.panels")

__all__ = [
    "OutputPanel",
    "ReplPanel",
    "ReplVariable",
    "TerminalPanel",
    "CodeToolsPanel",
    "AiPanel",
    "LintWidget",
    "GitPanel",
    "GitWidget",
    "PackageManagerWidget",

    "LogWidget",
    "LogPanel",
    "DebugPanel",
    "InternalsPanel",
    "TestRunnerPanel",
    "DependencyStudioPanel",
]

_SEVERITY_COLOR_KEYS: dict[str, str] = {
    "error": "red",
    "warning": "yellow",
    "convention": "purple",
    "info": "blue",
}


def _severity_color(palette: ColorPalette, severity_value: str) -> str:
    """Return the palette hex color that best represents a severity level."""
    color_key = _SEVERITY_COLOR_KEYS.get(severity_value, "muted")
    return getattr(palette, color_key, palette.text)


def _section_label(text: str, parent: QWidget | None = None) -> QLabel:
    """Build a small bold section-title label used above ungrouped controls."""
    label = QLabel(text, parent)
    label.setProperty("role", "section-title")
    return label


def _qcolor(hex_value: str):
    """Import and return a QColor lazily to prevent top-level dependency cycles."""
    from PySide6.QtGui import QColor

    return QColor(hex_value)


