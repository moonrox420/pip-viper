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
# Package Manager Panel
# -----------------------------------------------------------------------------


class PackageManagerWidget(QWidget):
    """Local pip package inventory manager with pip process signals."""

    install_requested = Signal(str)
    uninstall_requested = Signal(str)
    refresh_requested = Signal()

    def __init__(
        self,
        palette: ColorPalette,
        python_executable: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._python: str = python_executable
        self._all_packages: list[PipPackage] = []

        self._filter_input = QLineEdit(self)
        self._filter_input.setPlaceholderText("Filter packages by name...")
        self._filter_input.textChanged.connect(self._apply_filter)

        self._table = QTableWidget(0, 3, self)
        self._table.setHorizontalHeaderLabels(["Package", "Version", "Location"])
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setColumnWidth(0, 200)
        self._table.setColumnWidth(1, 120)

        self._install_input = QLineEdit(self)
        self._install_input.setPlaceholderText("package name")
        install_button = QPushButton("⬇ Install", self)
        install_button.setProperty("role", "primary")
        install_button.clicked.connect(self._on_install)
        uninstall_button = QPushButton("⬆ Uninstall", self)
        uninstall_button.clicked.connect(self._on_uninstall)
        refresh_button = QPushButton("🔄 Refresh", self)
        refresh_button.clicked.connect(self.refresh_requested)

        controls_layout = QHBoxLayout()
        controls_layout.addWidget(self._install_input, 1)
        controls_layout.addWidget(install_button)
        controls_layout.addWidget(uninstall_button)
        controls_layout.addWidget(refresh_button)

        environment_box = QGroupBox("📦 Environment Packages", self)
        environment_layout = QVBoxLayout(environment_box)
        environment_layout.addWidget(self._filter_input)
        environment_layout.addWidget(self._table)
        environment_layout.addLayout(controls_layout)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(environment_box)

    def set_palette(self, palette: ColorPalette) -> None:
        """Update internal palette referential bounds."""
        self._palette = palette

    @Slot()
    def _on_install(self) -> None:
        name = self._install_input.text().strip()
        if not name:
            return

        from ..services.offline_service import OfflineService
        offline_service = OfflineService.get_instance()
        if offline_service.is_offline() and not offline_service.get_local_wheelhouse_dir():
            QMessageBox.warning(
                self,
                "Offline Mode Active",
                f"Cannot install '{name}': PipViper is currently operating in strict Offline Mode.\n\n"
                f"To install packages:\n"
                f"• Switch to Online Mode (Opt-in) via the status bar.\n"
                f"• Or configure a Local Wheelhouse directory for offline installation."
            )
            return

        self.install_requested.emit(name)
        self._install_input.clear()

    @Slot()
    def _on_uninstall(self) -> None:
        item = self._table.currentItem()
        if item is None:
            return
        row_index = item.row()
        name_item = self._table.item(row_index, 0)
        if name_item is not None:
            self.uninstall_requested.emit(name_item.text())

    def populate(self, packages: Sequence[PipPackage]) -> None:
        """Display discovered packages in the active workspace."""
        self._all_packages = list(packages)
        self._render(self._all_packages)

    def _apply_filter(self, filter_text: str) -> None:
        stripped = filter_text.strip().lower()
        if not stripped:
            self._render(self._all_packages)
            return
        filtered = [
            package
            for package in self._all_packages
            if stripped in package.name.lower()
        ]
        self._render(filtered)

    def _render(self, packages: Sequence[PipPackage]) -> None:
        self._table.setRowCount(len(packages))
        for row_index, pip_package in enumerate(packages):
            self._table.setItem(row_index, 0, QTableWidgetItem(pip_package.name))
            self._table.setItem(row_index, 1, QTableWidgetItem(pip_package.version))
            self._table.setItem(
                row_index, 2, QTableWidgetItem(str(pip_package.location))
            )


