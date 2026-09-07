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
# Ai Panel (Local Assistant)
# -----------------------------------------------------------------------------


class AiPanel(QWidget):
    """An interactive chat and code-refactoring console communicating with local AI."""

    chat_submitted = Signal(str, str, str)  # Emits: host, model, query_text
    refactor_requested = Signal(str, str)  # Emits: host, model

    def __init__(self, palette: ColorPalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette: ColorPalette = palette

        self._status_label = QLabel("Status: LOCAL AI AGENT IDLE", self)
        self._status_label.setStyleSheet(f"color: {palette.muted}; font-weight: bold;")

        self._host_input = QLineEdit(self)
        self._host_input.setText("http://localhost:11434/v1")
        self._host_input.setPlaceholderText("Ollama / llama.cpp base url")

        self._model_input = QLineEdit(self)
        self._model_input.setText("qwen2.5-coder:7b-instruct-q8_0")
        self._model_input.setPlaceholderText("Model identifier tag")

        self._refactor_button = QPushButton("🤖 AI Refactor Active File", self)
        self._refactor_button.setProperty("role", "primary")
        self._refactor_button.clicked.connect(self._on_refactor)

        config_box = QGroupBox("⚙ Local AI Sidecar", self)
        config_layout = QVBoxLayout(config_box)
        config_layout.addWidget(QLabel("Host URL:", self))
        config_layout.addWidget(self._host_input)
        config_layout.addWidget(QLabel("Model Tag:", self))
        config_layout.addWidget(self._model_input)
        config_layout.addWidget(self._refactor_button)

        # Chat and output console
        self._chat_history = QPlainTextEdit(self)
        self._chat_history.setReadOnly(True)
        self._chat_history.setFont(QFont("Consolas", 10))
        self._chat_history.setPlaceholderText(
            "Local sandbox conversations and execution outputs will print here..."
        )

        self._apply_colors()

        self._query_input = QLineEdit(self)
        self._query_input.setPlaceholderText(
            "Ask the local AI a Python question, or request adjustments..."
        )
        self._query_input.returnPressed.connect(self._on_submit)

        self._send_button = QPushButton("💬 Send", self)
        self._send_button.clicked.connect(self._on_submit)

        input_layout = QHBoxLayout()
        input_layout.addWidget(self._query_input, 1)
        input_layout.addWidget(self._send_button)

        conversation_box = QGroupBox("💬 Conversation", self)
        conversation_layout = QVBoxLayout(conversation_box)
        conversation_layout.addWidget(self._chat_history)
        conversation_layout.addLayout(input_layout)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._status_label)
        layout.addWidget(config_box)
        layout.addWidget(conversation_box, 1)

    def set_palette(self, palette: ColorPalette) -> None:
        """Update AI agent colors on editor theme switches."""
        self._palette = palette
        self._apply_colors()

    def _apply_colors(self) -> None:
        self._chat_history.setStyleSheet(
            f"background-color: {self._palette.background}; color: {self._palette.text};"
        )

    def log_response(self, sender: str, text: str) -> None:
        """Render communication lines cleanly within the history panel."""
        self._chat_history.appendPlainText(f"\n[{sender}]: {text}")

    def set_busy(self, is_busy: bool) -> None:
        """Block controls to preserve execution pipeline constraints."""
        self._query_input.setEnabled(not is_busy)
        self._send_button.setEnabled(not is_busy)
        self._refactor_button.setEnabled(not is_busy)
        if is_busy:
            self._status_label.setText("Status: AI GENERATING RESPONSE...")
            self._status_label.setStyleSheet(
                f"color: {self._palette.yellow}; font-weight: bold;"
            )
        else:
            self._status_label.setText("Status: LOCAL AI AGENT IDLE")
            self._status_label.setStyleSheet(
                f"color: {self._palette.muted}; font-weight: bold;"
            )

    @Slot()
    def _on_submit(self) -> None:
        query_text = self._query_input.text().strip()
        if not query_text:
            return
        self.log_response("You", query_text)
        self._query_input.clear()
        self.set_busy(True)
        self.chat_submitted.emit(
            self._host_input.text().strip(),
            self._model_input.text().strip(),
            query_text,
        )

    @Slot()
    def _on_refactor(self) -> None:
        self.set_busy(True)
        self.refactor_requested.emit(
            self._host_input.text().strip(),
            self._model_input.text().strip(),
        )


