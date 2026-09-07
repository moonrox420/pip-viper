"""Side-panel workspace widgets: File Explorer and Document Outline.

This module provides standard navigation widgets for exploring workspace directories
and viewing structured hierarchical outlines of standard Python documents.
"""

from __future__ import annotations

import ast
import gc
import logging
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QDir, QModelIndex, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCursor, QFont
from PySide6.QtWidgets import (
    QFileSystemModel,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .memory_tracker import ProcessMemorySampler

_LOGGER: logging.Logger = logging.getLogger("src.widgets")

__all__ = [
    "FileExplorer",
    "DocumentOutlineWidget",
    "MemoryMeterWidget",
    "OfflineModeBadge",
]

_OUTLINE_ICONS: dict[str, str] = {
    "class": "📦",
    "function": "ƒ",
    "async_function": "⚡",
    "method": "◦ƒ",
    "async_method": "◦⚡",
}


class FileExplorer(QWidget):
    """A hierarchical tree view of files and folders inside the workspace directory.

    Includes a lightweight filename filter field above the tree so large
    workspaces stay easy to scan.
    """

    file_activated = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = QFileSystemModel(self)
        self._model.setFilter(
            QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Files
        )
        self._model.setNameFilterDisables(False)

        self._header_label = QLabel("WORKSPACE", self)
        self._header_label.setProperty("role", "section-title")

        self._filter_input: QLineEdit = QLineEdit(self)
        self._filter_input.setPlaceholderText("Filter files (e.g. *.py)")
        self._filter_input.textChanged.connect(self._on_filter_changed)

        self._view = QTreeView(self)
        self._view.setModel(self._model)
        self._view.setHeaderHidden(True)
        self._view.setAlternatingRowColors(True)

        # Hide file size, file type, and modification dates to keep sidebar minimal
        for column_index in range(1, 4):
            self._view.hideColumn(column_index)

        self._view.activated.connect(self._on_activated)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(self._header_label)
        layout.addWidget(self._filter_input)
        layout.addWidget(self._view)

        # Keep the tree usable even when the sidebar splitter is dragged small.
        self.setMinimumWidth(200)
        self.setMinimumHeight(150)

    def set_root(self, folder: Path) -> None:
        """Set the active workspace directory to monitor.

        Args:
            folder: The Path pointing to the workspace root directory.
        """
        self._model.setRootPath(str(folder))
        self._view.setRootIndex(self._model.index(str(folder)))
        self._header_label.setText(f"WORKSPACE: {folder.name or str(folder)}")

    @Slot(str)
    def _on_filter_changed(self, filter_text: str) -> None:
        stripped = filter_text.strip()
        if not stripped:
            self._model.setNameFilters([])
            return
        patterns = [
            pattern.strip() for pattern in stripped.split(",") if pattern.strip()
        ]
        if not any("*" in pattern or "?" in pattern for pattern in patterns):
            patterns = [f"*{pattern}*" for pattern in patterns]
        self._model.setNameFilters(patterns)

    @Slot(QModelIndex)
    def _on_activated(self, index: QModelIndex) -> None:
        file_path_string = self._model.filePath(index)
        path = Path(file_path_string)
        if path.is_file():
            self.file_activated.emit(path)


class DocumentOutlineWidget(QWidget):
    """A structural overview panel representing definitions inside a Python source file.

    Definitions are rendered in source order and indented to reflect nesting depth,
    so methods appear grouped under their owning class rather than interleaved
    with unrelated top-level symbols.
    """

    item_selected = Signal(int, int)  # line, column (1-based indices)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._header_label = QLabel("OUTLINE", self)
        self._header_label.setProperty("role", "section-title")

        self._list = QListWidget(self)
        self._list.setFont(QFont("Consolas", 10))
        self._list.itemClicked.connect(self._on_clicked)

        self._hint_label = QLabel("Open a Python file to see its outline.", self)
        self._hint_label.setProperty("role", "hint")
        self._hint_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(self._header_label)
        layout.addWidget(self._list)
        layout.addWidget(self._hint_label)

        # Keep the outline usable even when the sidebar splitter is dragged small.
        self.setMinimumWidth(200)
        self.setMinimumHeight(120)

        self._show_hint("Open a Python file to see its outline.")

    def update_from_source(self, source: str) -> None:
        """Reparse Python code using ast and refresh the list overview.

        Args:
            source: The raw Python source code string.
        """
        self._list.clear()
        if not source.strip():
            self._show_hint("Open a Python file to see its outline.")
            return

        try:
            syntax_tree = ast.parse(source)
        except SyntaxError as exception:
            _LOGGER.debug(
                "Document outline skipped - syntax parsing error: %s", exception
            )
            self._show_hint("Outline unavailable: fix the syntax error to continue.")
            return

        entries: list[tuple[int, int, ast.AST]] = []
        self._collect_entries(syntax_tree.body, depth=0, entries=entries)

        if not entries:
            self._show_hint("No classes or functions found in this file.")
            return

        self._hint_label.hide()
        self._list.show()
        for depth, _line, node in entries:
            self._add_entry(node, depth)

    def _show_hint(self, message: str) -> None:
        """Hide the (now-empty) list and surface an explanatory hint instead."""
        self._hint_label.setText(message)
        self._hint_label.show()
        self._list.hide()

    def _collect_entries(
        self,
        body: list[ast.stmt],
        depth: int,
        entries: list[tuple[int, int, ast.AST]],
    ) -> None:
        """Recursively walk a statement body in source order, tracking depth."""
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                entries.append((depth, node.lineno, node))
                self._collect_entries(node.body, depth + 1, entries)

    def _add_entry(self, node: ast.AST, depth: int) -> None:
        if isinstance(node, ast.ClassDef):
            icon = _OUTLINE_ICONS["class"]
            label = node.name
        elif isinstance(node, ast.AsyncFunctionDef):
            icon = _OUTLINE_ICONS["async_method" if depth > 0 else "async_function"]
            label = f"{node.name}()"
        elif isinstance(node, ast.FunctionDef):
            icon = _OUTLINE_ICONS["method" if depth > 0 else "function"]
            label = f"{node.name}()"
        else:  # pragma: no cover
            return

        indent = "    " * depth
        list_item = QListWidgetItem(f"{indent}{icon}  {label}")
        list_item.setData(Qt.ItemDataRole.UserRole, (node.lineno, node.col_offset))
        self._list.addItem(list_item)

    @Slot(QListWidgetItem)
    def _on_clicked(self, item: QListWidgetItem) -> None:
        item_data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(item_data, tuple) and len(item_data) == 2:
            line_index, column_index = item_data
            self.item_selected.emit(int(line_index), int(column_index))


# -----------------------------------------------------------------------------
# Status Bar Memory Meter Widget
# -----------------------------------------------------------------------------


class MemoryMeterWidget(QWidget):
    """Status bar widget displaying real-time process memory consumption.

    Delivers live memory telemetry, low-overhead background polling,
    a one-click Garbage Collection action, and navigation to the Memory Studio.
    """

    open_memory_profiler_requested = Signal()
    gc_collected = Signal(int)

    def __init__(
        self,
        palette: Optional[Any] = None,
        poll_interval_ms: int = 2500,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._poll_interval_ms = poll_interval_ms

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)

        self._label = QLabel("💾 0.0 MB", self)
        self._label.setFont(QFont("Segoe UI", 9))
        self._label.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self._label)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update_memory)
        self._timer.start(self._poll_interval_ms)

        self.set_palette(palette)
        self.update_memory()

    def set_palette(self, palette: Any) -> None:
        self._palette = palette
        if palette:
            text_color = getattr(palette, "muted", "#888888")
            self._label.setStyleSheet(f"color: {text_color}; font-weight: bold;")

    def update_memory(self) -> None:
        """Sample process memory and update label and tooltip."""
        info = ProcessMemorySampler.get_memory_info()
        self._label.setText(f"💾 {info.working_set_mb} MB")

        tooltip = (
            f"PipViper Memory Telemetry\n"
            f"• Working Set (RSS): {info.working_set_mb} MB\n"
            f"• Peak Working Set: {info.peak_working_set_mb} MB\n"
            f"• Private Memory: {info.private_mb} MB\n"
            f"• Python Allocated Blocks: {info.allocated_blocks:,}\n\n"
            f"Click for actions • Double-click for Memory Studio"
        )
        self.setToolTip(tooltip)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._show_actions_menu()

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_memory_profiler_requested.emit()

    def _show_actions_menu(self) -> None:
        menu = QMenu(self)
        info = ProcessMemorySampler.get_memory_info()

        title_action = menu.addAction(f"Process Memory: {info.working_set_mb} MB (Peak: {info.peak_working_set_mb} MB)")
        title_action.setEnabled(False)
        menu.addSeparator()

        gc_action = menu.addAction("🧹 Run Garbage Collection (gc.collect())")
        gc_action.triggered.connect(self._run_gc)

        open_action = menu.addAction("🔍 Open Memory Diagnostics Studio")
        open_action.triggered.connect(self.open_memory_profiler_requested.emit)

        menu.exec(QCursor.pos())

    def _run_gc(self) -> None:
        collected = gc.collect()
        self.update_memory()
        self.gc_collected.emit(collected)

    def closeEvent(self, event: Any) -> None:
        self._timer.stop()
        super().closeEvent(event)


class OfflineModeBadge(QWidget):
    """Status bar / toolbar badge reflecting global Offline Mode state (PRD U1)."""

    offline_mode_toggled = Signal(bool)

    def __init__(self, palette: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.addWidget(self._label)

        self.setCursor(Qt.CursorShape.PointingHandCursor)

        from .services.offline_service import OfflineService
        self._service = OfflineService.get_instance()
        self._service.offline_mode_changed.connect(self._on_offline_changed)

        self.update_state()

    def set_palette(self, palette: Any) -> None:
        self._palette = palette
        self.update_state()

    def update_state(self) -> None:
        is_offline = self._service.is_offline()
        if is_offline:
            self._label.setText("🔒 Offline Mode")
            accent = getattr(self._palette, "green", "#2ea44f") if self._palette else "#2ea44f"
            self._label.setStyleSheet(f"color: {accent}; font-weight: bold; font-size: 11px;")
            self.setToolTip(
                "PipViper Offline Mode: ACTIVE\n"
                "• All outbound network sockets are blocked.\n"
                "• Package discovery and AST analysis run locally.\n"
                "• AI assistant connects only to local sidecar (localhost).\n\n"
                "Click to toggle or configure local wheelhouse."
            )
        else:
            self._label.setText("🌐 Online (Opt-in)")
            warning_color = getattr(self._palette, "yellow", "#d29922") if self._palette else "#d29922"
            self._label.setStyleSheet(f"color: {warning_color}; font-weight: bold; font-size: 11px;")
            self.setToolTip(
                "PipViper Online Mode: OPT-IN ACTIVE\n"
                "• External PyPI checks and package installs are allowed.\n"
                "Click to switch back to strict Offline Mode."
            )

    def _on_offline_changed(self, is_offline: bool) -> None:
        self.update_state()
        self.offline_mode_toggled.emit(is_offline)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._show_toggle_menu()

    def _show_toggle_menu(self) -> None:
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        menu = QMenu(self)
        current_offline = self._service.is_offline()

        status_action = menu.addAction(f"Mode: {'🔒 Offline' if current_offline else '🌐 Online (Opt-in)'}")
        status_action.setEnabled(False)
        menu.addSeparator()

        if current_offline:
            toggle_action = menu.addAction("🌐 Switch to Online Mode (Opt-in)...")
            def do_enable_online() -> None:
                confirm = QMessageBox.question(
                    self,
                    "Enable Online Mode?",
                    "Are you sure you want to enable Online Mode?\n\n"
                    "This allows PyPI package updates and network package installs.\n"
                    "Core IDE features will continue to operate normally.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if confirm == QMessageBox.StandardButton.Yes:
                    self._service.set_offline_mode(False)
            toggle_action.triggered.connect(do_enable_online)
        else:
            toggle_action = menu.addAction("🔒 Switch to Strict Offline Mode")
            toggle_action.triggered.connect(lambda: self._service.set_offline_mode(True))

        menu.addSeparator()
        wheel_action = menu.addAction("📦 Configure Local Wheelhouse Directory...")
        def choose_wheelhouse() -> None:
            current = self._service.get_local_wheelhouse_dir()
            initial_dir = str(current) if current else ""
            selected_dir = QFileDialog.getExistingDirectory(
                self, "Select Local Wheel / Package Cache Directory", initial_dir
            )
            if selected_dir:
                self._service.set_local_wheelhouse_dir(Path(selected_dir))
        wheel_action.triggered.connect(choose_wheelhouse)

        menu.exec(QCursor.pos())


