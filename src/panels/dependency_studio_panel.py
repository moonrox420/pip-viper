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
# Dependency Studio Panel (Phase 8)
# -----------------------------------------------------------------------------


class DependencyStudioPanel(QWidget):
    """Interactive environment dependency visualizer, graph inspector, and package manager."""

    upgrade_requested = Signal(str)
    uninstall_requested = Signal(str)
    refresh_requested = Signal()
    sync_requirements_requested = Signal()
    export_graph_requested = Signal(str)

    def __init__(self, palette: ColorPalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette: ColorPalette = palette
        self._graph: DependencyGraph = DependencyGraph()
        self._active_selected_node: DependencyNode | None = None

        # 1. Top Toolbar
        self._lbl_title = QLabel("🕸 Dependency Studio", self)
        self._lbl_title.setStyleSheet("font-weight: bold; font-size: 13px;")

        self._view_mode_combo = QComboBox(self)
        self._view_mode_combo.addItems(["🌳 Hierarchy Tree", "📋 Flat Inventory", "🟡 Outdated Only"])
        self._view_mode_combo.currentIndexChanged.connect(self._render_tree)

        self._filter_input = QLineEdit(self)
        self._filter_input.setPlaceholderText("Filter packages by name...")
        self._filter_input.textChanged.connect(self._apply_filter)

        self._btn_refresh = QPushButton("🔄 Refresh", self)
        self._btn_refresh.clicked.connect(self.refresh_requested.emit)

        self._btn_check_updates = QPushButton("🔍 Check PyPI Updates (Requires Network)", self)
        self._btn_check_updates.clicked.connect(self._check_pypi_updates)

        self._btn_sync_reqs = QPushButton("📄 Sync requirements.txt", self)
        self._btn_sync_reqs.clicked.connect(self.sync_requirements_requested.emit)

        self._btn_export_mermaid = QPushButton("📊 Mermaid Export", self)
        self._btn_export_mermaid.clicked.connect(self._on_export_mermaid)

        btn_zoom_in = QPushButton("+", self)
        btn_zoom_in.setFixedWidth(28)
        btn_zoom_out = QPushButton("-", self)
        btn_zoom_out.setFixedWidth(28)
        btn_reset_zoom = QPushButton("⟲", self)
        btn_reset_zoom.setFixedWidth(28)

        toolbar_layout = QHBoxLayout()
        toolbar_layout.setContentsMargins(6, 4, 6, 4)
        toolbar_layout.setSpacing(6)
        toolbar_layout.addWidget(self._lbl_title)
        toolbar_layout.addWidget(self._view_mode_combo)
        toolbar_layout.addWidget(self._filter_input, 1)
        toolbar_layout.addWidget(self._btn_refresh)
        toolbar_layout.addWidget(self._btn_check_updates)
        toolbar_layout.addWidget(self._btn_sync_reqs)
        toolbar_layout.addWidget(self._btn_export_mermaid)
        toolbar_layout.addWidget(btn_zoom_in)
        toolbar_layout.addWidget(btn_zoom_out)
        toolbar_layout.addWidget(btn_reset_zoom)

        # 2. Main Three-Pane Splitter
        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Pane A: Tree Hierarchy
        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels(["Package", "Installed", "Status"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._tree.itemClicked.connect(self._on_tree_item_clicked)
        self._tree.itemDoubleClicked.connect(self._on_tree_item_double_clicked)

        # Pane B: Visual Node-Link Canvas
        self._graph_view = DependencyGraphView(self._palette, self)
        self._graph_view.node_selected.connect(self._on_graph_node_selected)
        btn_zoom_in.clicked.connect(self._graph_view.zoom_in)
        btn_zoom_out.clicked.connect(self._graph_view.zoom_out)
        btn_reset_zoom.clicked.connect(self._graph_view.reset_view)

        # Pane C: Package Inspector Card
        self._inspector_panel = QWidget(self)
        inspector_layout = QVBoxLayout(self._inspector_panel)
        inspector_layout.setContentsMargins(8, 8, 8, 8)
        inspector_layout.setSpacing(6)

        self._lbl_pkg_name = QLabel("No Package Selected", self._inspector_panel)
        self._lbl_pkg_name.setStyleSheet("font-weight: bold; font-size: 13px;")
        self._lbl_pkg_version = QLabel("", self._inspector_panel)
        self._lbl_pkg_summary = QLabel("Click any package in the tree or graph to inspect.", self._inspector_panel)
        self._lbl_pkg_summary.setWordWrap(True)

        self._lbl_pkg_license = QLabel("", self._inspector_panel)
        self._lbl_pkg_author = QLabel("", self._inspector_panel)
        self._lbl_pkg_homepage = QLabel("", self._inspector_panel)
        self._lbl_pkg_homepage.setOpenExternalLinks(True)

        lbl_requires = QLabel("Direct Dependencies (Requires):", self._inspector_panel)
        lbl_requires.setStyleSheet("font-weight: bold;")
        self._list_requires = QListWidget(self._inspector_panel)
        self._list_requires.setMaximumHeight(90)
        self._list_requires.itemDoubleClicked.connect(self._on_dependency_list_double_clicked)

        lbl_required_by = QLabel("Reverse Dependents (Required By):", self._inspector_panel)
        lbl_required_by.setStyleSheet("font-weight: bold;")
        self._list_required_by = QListWidget(self._inspector_panel)
        self._list_required_by.setMaximumHeight(90)
        self._list_required_by.itemDoubleClicked.connect(self._on_dependency_list_double_clicked)

        self._btn_upgrade = QPushButton("⬆ Upgrade Package", self._inspector_panel)
        self._btn_upgrade.clicked.connect(self._on_upgrade_clicked)
        self._btn_upgrade.setEnabled(False)

        self._btn_uninstall = QPushButton("🗑 Uninstall Package", self._inspector_panel)
        self._btn_uninstall.clicked.connect(self._on_uninstall_clicked)
        self._btn_uninstall.setEnabled(False)

        actions_layout = QHBoxLayout()
        actions_layout.addWidget(self._btn_upgrade)
        actions_layout.addWidget(self._btn_uninstall)

        inspector_layout.addWidget(self._lbl_pkg_name)
        inspector_layout.addWidget(self._lbl_pkg_version)
        inspector_layout.addWidget(self._lbl_pkg_summary)
        inspector_layout.addWidget(self._lbl_pkg_license)
        inspector_layout.addWidget(self._lbl_pkg_author)
        inspector_layout.addWidget(self._lbl_pkg_homepage)
        inspector_layout.addWidget(lbl_requires)
        inspector_layout.addWidget(self._list_requires)
        inspector_layout.addWidget(lbl_required_by)
        inspector_layout.addWidget(self._list_required_by)
        inspector_layout.addLayout(actions_layout)
        inspector_layout.addStretch(1)

        self._splitter.addWidget(self._tree)
        self._splitter.addWidget(self._graph_view)
        self._splitter.addWidget(self._inspector_panel)
        self._splitter.setSizes([260, 450, 240])

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)
        main_layout.addLayout(toolbar_layout)
        main_layout.addWidget(self._splitter, 1)

        self.set_palette(self._palette)

    def set_graph(self, graph: DependencyGraph) -> None:
        """Update active dependency graph and refresh tree and visual canvas."""
        self._graph = graph
        self._render_tree()
        self._graph_view.populate(graph)
        self._update_inspector(None)

    def set_palette(self, palette: ColorPalette) -> None:
        """Apply active visual styling across panel components."""
        self._palette = palette
        self._graph_view.set_palette(palette)
        css = (
            f"QWidget {{ background-color: {palette.background}; color: {palette.text}; }}"
            f"QTreeWidget, QListWidget {{ background-color: {palette.panel}; color: {palette.text}; "
            f"border: 1px solid {palette.border}; border-radius: 4px; }}"
            f"QTreeWidget::item:selected, QListWidget::item:selected {{ background-color: {palette.selection}; color: {palette.text}; }}"
            f"QLineEdit, QComboBox {{ background-color: {palette.panel}; color: {palette.text}; "
            f"border: 1px solid {palette.border}; padding: 3px 6px; border-radius: 4px; }}"
            f"QPushButton {{ background-color: {palette.panel}; color: {palette.text}; "
            f"border: 1px solid {palette.border}; padding: 3px 8px; border-radius: 4px; }}"
            f"QPushButton:hover {{ background-color: {palette.selection}; }}"
            f"QPushButton:disabled {{ color: {palette.muted}; border-color: {palette.border}; }}"
        )
        self.setStyleSheet(css)

    def _render_tree(self) -> None:
        """Populate left tree view according to active view mode."""
        self._tree.clear()
        mode_idx = self._view_mode_combo.currentIndex()
        filter_text = self._filter_input.text().strip().lower()

        if mode_idx == 0:
            # Hierarchy Tree: Top-Level Packages -> Dependencies
            roots = self._graph.top_level_packages
            for root_name in roots:
                node = self._graph.get_node(root_name)
                if not node:
                    continue
                if filter_text and filter_text not in node.name.lower():
                    continue

                status_text = "🟡 Outdated" if node.is_outdated else "🟢 Up to date"
                root_item = QTreeWidgetItem([f"📦 {node.name}", f"v{node.installed_version}", status_text])
                root_item.setData(0, Qt.ItemDataRole.UserRole, node)
                self._tree.addTopLevelItem(root_item)

                # Add direct dependencies as children
                for dep_name in node.clean_requires_names:
                    dep_node = self._graph.get_node(dep_name)
                    if dep_node:
                        dep_status = "🟡 Outdated" if dep_node.is_outdated else "🟢 Up to date"
                        child_item = QTreeWidgetItem([f"↳ {dep_node.name}", f"v{dep_node.installed_version}", dep_status])
                        child_item.setData(0, Qt.ItemDataRole.UserRole, dep_node)
                    else:
                        child_item = QTreeWidgetItem([f"↳ {dep_name}", "uninstalled", "⚠️ Missing"])
                        child_item.setData(0, Qt.ItemDataRole.UserRole, None)
                    root_item.addChild(child_item)

        elif mode_idx == 1:
            # Flat Inventory
            for pkg_name in self._graph.all_packages:
                node = self._graph.get_node(pkg_name)
                if not node:
                    continue
                if filter_text and filter_text not in node.name.lower():
                    continue
                status_text = "🟡 Outdated" if node.is_outdated else "🟢 Up to date"
                item = QTreeWidgetItem([node.name, f"v{node.installed_version}", status_text])
                item.setData(0, Qt.ItemDataRole.UserRole, node)
                self._tree.addTopLevelItem(item)

        elif mode_idx == 2:
            # Outdated Only
            for pkg_name in self._graph.all_packages:
                node = self._graph.get_node(pkg_name)
                if not node or not node.is_outdated:
                    continue
                if filter_text and filter_text not in node.name.lower():
                    continue
                ver_text = f"v{node.installed_version} → {node.latest_version or 'newer'}"
                item = QTreeWidgetItem([node.name, ver_text, "🟡 Outdated"])
                item.setData(0, Qt.ItemDataRole.UserRole, node)
                self._tree.addTopLevelItem(item)

    def _apply_filter(self) -> None:
        self._render_tree()

    @Slot(QTreeWidgetItem, int)
    def _on_tree_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(node, DependencyNode):
            self._update_inspector(node)
            self._graph_view.select_package(node.name)

    @Slot(QTreeWidgetItem, int)
    def _on_tree_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        self._on_tree_item_clicked(item, column)

    @Slot(object)
    def _on_graph_node_selected(self, node: DependencyNode) -> None:
        self._update_inspector(node)

    def _update_inspector(self, node: DependencyNode | None) -> None:
        """Display package metadata and connections inside the inspector card."""
        self._active_selected_node = node
        self._list_requires.clear()
        self._list_required_by.clear()

        if not node:
            self._lbl_pkg_name.setText("No Package Selected")
            self._lbl_pkg_version.setText("")
            self._lbl_pkg_summary.setText("Click any package in the tree or graph to inspect.")
            self._lbl_pkg_license.setText("")
            self._lbl_pkg_author.setText("")
            self._lbl_pkg_homepage.setText("")
            self._btn_upgrade.setEnabled(False)
            self._btn_uninstall.setEnabled(False)
            return

        self._lbl_pkg_name.setText(f"📦 {node.name}")
        ver_text = f"Installed: v{node.installed_version}"
        if node.latest_version:
            ver_text += f" | PyPI Latest: v{node.latest_version}"
        self._lbl_pkg_version.setText(ver_text)

        self._lbl_pkg_summary.setText(node.summary or "No package description available.")
        self._lbl_pkg_license.setText(f"License: {node.license}" if node.license else "")
        self._lbl_pkg_author.setText(f"Author: {node.author}" if node.author else "")
        if node.homepage:
            self._lbl_pkg_homepage.setText(f"<a href='{node.homepage}' style='color: {self._palette.blue};'>🌐 Project Homepage</a>")
        else:
            self._lbl_pkg_homepage.setText("")

        for req in node.requires:
            self._list_requires.addItem(req)

        for dep in node.required_by:
            self._list_required_by.addItem(dep)

        self._btn_upgrade.setEnabled(node.is_outdated)
        self._btn_uninstall.setEnabled(True)

    @Slot(QListWidgetItem)
    def _on_dependency_list_double_clicked(self, item: QListWidgetItem) -> None:
        clean_name = re.split(r"[><=~!;]", item.text())[0].strip()
        node = self._graph.get_node(clean_name)
        if node:
            self._update_inspector(node)
            self._graph_view.select_package(node.name)

    @Slot()
    def _on_upgrade_clicked(self) -> None:
        if self._active_selected_node:
            self.upgrade_requested.emit(self._active_selected_node.name)

    @Slot()
    def _on_uninstall_clicked(self) -> None:
        if self._active_selected_node:
            self.uninstall_requested.emit(self._active_selected_node.name)

    @Slot()
    def _on_export_mermaid(self) -> None:
        mermaid_text = self._graph.to_mermaid()
        clipboard = QGuiApplication.clipboard()
        if clipboard:
            clipboard.setText(mermaid_text)
        self.export_graph_requested.emit(mermaid_text)

    def _check_pypi_updates(self) -> None:
        """Query PyPI asynchronously to identify outdated packages."""
        from ..services.offline_service import OfflineService
        if OfflineService.get_instance().is_offline():
            QMessageBox.information(
                self,
                "Offline Mode Active",
                "PyPI update checks are disabled because PipViper is running in Offline Mode.\n\n"
                "To check for package updates, switch to Online Mode (Opt-in) in the status bar.",
            )
            return

        scanner = DependencyScanner()
        packages = self._graph.all_packages

        def worker() -> None:
            updated_count = 0
            for pkg in packages:
                latest = scanner.check_pypi_update(pkg)
                node = self._graph.get_node(pkg)
                if node and latest:
                    node.latest_version = latest
                    if latest != node.installed_version:
                        node.is_outdated = True
                        updated_count += 1
            # Re-render UI back on Qt main thread
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._on_pypi_check_finished(updated_count))

        run_in_thread(worker)

    def _on_pypi_check_finished(self, updated_count: int) -> None:
        self._render_tree()
        if self._active_selected_node:
            self._update_inspector(self._active_selected_node)
        self._graph_view.populate(self._graph)


