"""High-performance workspace navigation and code intelligence dialogs.

Provides:
- FuzzyMatcher: Subsequence scoring and ranking algorithm for instant fuzzy search.
- QuickOpenDialog: Floating spotlight file finder modal (Ctrl+P) with line jump (:line).
- CommandPaletteDialog: Universal searchable command launcher (Ctrl+Shift+P).
- SearchInFilesWidget & SearchInFilesWorker: Multi-threaded project-wide regex search tab (Ctrl+Shift+H).
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QKeyEvent, QKeySequence, QPainter, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

_LOGGER = logging.getLogger("src.navigation")

# Common folders and files to ignore during workspace traversal
DEFAULT_IGNORED_DIRS: set[str] = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".tox",
    "build",
    "dist",
    ".idea",
    ".vscode",
    ".eggs",
}

DEFAULT_IGNORED_EXTENSIONS: set[str] = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".zip",
    ".tar",
    ".gz",
    ".whl",
    ".pdf",
}


# =============================================================================
# 1. Fuzzy Matching Engine
# =============================================================================


class FuzzyMatcher:
    """Subsequence fuzzy matching and scoring engine.

    Calculates relevance scores based on:
    - Subsequence match continuity (consecutive characters rewarded)
    - Word boundary matches (following '/', '\\', '_', '-', '.', or camelCase)
    - File basename proximity (matches in filename scored higher than in path)
    - Shorter path penalty (concise matching candidates preferred)
    """

    @staticmethod
    def parse_query_line(query: str) -> tuple[str, int | None]:
        """Parse query string for optional line number jump syntax (e.g. 'app.py:120')."""
        query = query.strip()
        if ":" in query:
            parts = query.rsplit(":", 1)
            if parts[1].isdigit():
                return parts[0].strip(), int(parts[1])
        return query, None

    @classmethod
    def match(cls, pattern: str, target: str) -> tuple[bool, int, list[int]]:
        """Determine if pattern is a subsequence of target and return score + match indices.

        Args:
            pattern: The search query string.
            target: The candidate string to match against.

        Returns:
            Tuple of (matched: bool, score: int, matched_indices: list[int]).
        """
        if not pattern:
            return True, 0, []

        p_len = len(pattern)
        t_len = len(target)
        if p_len > t_len:
            return False, 0, []

        pattern_lower = pattern.lower()
        target_lower = target.lower()

        # Quick check: all characters must be present in order
        p_idx = 0
        t_idx = 0
        indices: list[int] = []

        while p_idx < p_len and t_idx < t_len:
            if pattern_lower[p_idx] == target_lower[t_idx]:
                indices.append(t_idx)
                p_idx += 1
            t_idx += 1

        if p_idx < p_len:
            return False, 0, []

        # Calculate score
        score = 100

        # Exact match bonus
        if pattern_lower == target_lower:
            score += 1000
            return True, score, indices

        # Filename boundary bonus
        basename = os.path.basename(target)
        basename_lower = basename.lower()
        if pattern_lower in basename_lower:
            score += 300
            if basename_lower.startswith(pattern_lower):
                score += 200

        prev_idx = -2
        for i, idx in enumerate(indices):
            # Consecutive characters bonus
            if idx == prev_idx + 1:
                score += 35

            # Word boundary bonus
            if idx == 0 or target[idx - 1] in "/\\_-. ":
                score += 50
            # camelCase boundary bonus
            elif target[idx].isupper() and target[idx - 1].islower():
                score += 40

            # Exact case match bonus
            if pattern[i] == target[idx]:
                score += 15

            prev_idx = idx

        # Penalty for target length difference
        score -= min(150, (t_len - p_len) * 2)

        return True, max(1, score), indices

    @classmethod
    def filter_and_rank(
        cls, pattern: str, candidates: Sequence[str], limit: int = 50
    ) -> list[tuple[str, int, list[int]]]:
        """Filter and sort candidates by fuzzy relevance score."""
        results: list[tuple[str, int, list[int]]] = []
        clean_pattern = pattern.strip()

        for item in candidates:
            matched, score, indices = cls.match(clean_pattern, item)
            if matched:
                results.append((item, score, indices))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]


# =============================================================================
# 2. Quick Open Dialog (Ctrl+P)
# =============================================================================


class QuickOpenDelegate(QStyledItemDelegate):
    """Custom renderer for Quick Open list items displaying basename, path, and score."""

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: object
    ) -> None:
        painter.save()
        rect = option.rect  # type: ignore[attr-defined]

        # Background
        is_selected = option.state & QStyleOptionViewItem.StateFlag.State_Selected  # type: ignore[attr-defined]
        if is_selected:
            painter.fillRect(rect, option.palette.highlight())  # type: ignore[attr-defined]
        elif option.state & QStyleOptionViewItem.StateFlag.State_MouseOver:  # type: ignore[attr-defined]
            painter.fillRect(rect, QColor(255, 255, 255, 12))

        # Text data
        basename = str(index.data(Qt.ItemDataRole.UserRole) or "")  # type: ignore[attr-defined]
        rel_path = str(index.data(Qt.ItemDataRole.UserRole + 1) or "")  # type: ignore[attr-defined]

        # Fonts
        bold_font = QFont(option.font)  # type: ignore[attr-defined]
        bold_font.setBold(True)
        normal_font = QFont(option.font)  # type: ignore[attr-defined]

        metrics = QFontMetrics(bold_font)
        basename_width = metrics.horizontalAdvance(basename)

        # Draw Basename
        painter.setFont(bold_font)
        text_color = (
            option.palette.highlightedText().color()  # type: ignore[attr-defined]
            if is_selected
            else option.palette.text().color()  # type: ignore[attr-defined]
        )
        painter.setPen(text_color)
        painter.drawText(
            rect.x() + 10,
            rect.y() + metrics.ascent() + 6,
            basename,
        )

        # Draw Path
        painter.setFont(normal_font)
        muted_color = QColor(139, 148, 158) if not is_selected else text_color
        painter.setPen(muted_color)
        painter.drawText(
            rect.x() + 10 + basename_width + 12,
            rect.y() + metrics.ascent() + 6,
            rel_path,
        )

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: object) -> QSize:
        return QSize(option.rect.width() if hasattr(option, "rect") else 400, 32)  # type: ignore[attr-defined]


class QuickOpenDialog(QDialog):
    """Floating spotlight file finder modal dialog (Ctrl+P)."""

    file_selected = Signal(str, int)  # (absolute_file_path, line_number)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedWidth(640)
        self.setFixedHeight(380)

        self._workspace_root: Path | None = None
        self._indexed_files: list[str] = []  # relative paths
        self._abs_map: dict[str, str] = {}  # rel -> abs
        self._target_line: int = 1

        self._init_ui()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # Frame container with styled border
        frame = QFrame(self)
        frame.setObjectName("quickOpenFrame")
        frame.setStyleSheet(
            "#quickOpenFrame {"
            "  background-color: #161b22;"
            "  border: 1px solid #30363d;"
            "  border-radius: 6px;"
            "}"
        )
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(6, 6, 6, 6)
        frame_layout.setSpacing(6)

        # Search Bar
        self._search_input = QLineEdit(frame)
        self._search_input.setPlaceholderText("Search files by name (type :line to jump)...")
        self._search_input.setStyleSheet(
            "QLineEdit {"
            "  background-color: #0d1117;"
            "  color: #c9d1d9;"
            "  border: 1px solid #388bfd;"
            "  border-radius: 4px;"
            "  padding: 8px 12px;"
            "  font-size: 13px;"
            "}"
        )
        self._search_input.textChanged.connect(self._on_query_changed)
        frame_layout.addWidget(self._search_input)

        # Results List
        self._list_widget = QListWidget(frame)
        self._list_widget.setItemDelegate(QuickOpenDelegate(self))
        self._list_widget.setStyleSheet(
            "QListWidget {"
            "  background-color: #0d1117;"
            "  border: 1px solid #21262d;"
            "  border-radius: 4px;"
            "}"
            "QListWidget::item {"
            "  height: 32px;"
            "}"
        )
        self._list_widget.itemDoubleClicked.connect(self._on_item_activated)
        frame_layout.addWidget(self._list_widget)

        main_layout.addWidget(frame)

    def set_workspace_root(self, root: Path | None) -> None:
        """Set active workspace directory and index discoverable files."""
        self._workspace_root = root
        self._indexed_files.clear()
        self._abs_map.clear()

        if not root or not root.exists():
            return

        for current_root, dirs, files in os.walk(root):
            # Prune ignored directories in place
            dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORED_DIRS and not d.startswith(".")]

            for filename in files:
                ext = os.path.splitext(filename)[1].lower()
                if ext in DEFAULT_IGNORED_EXTENSIONS:
                    continue

                abs_path = os.path.join(current_root, filename)
                try:
                    rel_path = os.path.relpath(abs_path, root).replace("\\", "/")
                    self._indexed_files.append(rel_path)
                    self._abs_map[rel_path] = abs_path
                except ValueError:
                    continue

    def open_dialog(self) -> None:
        """Center the modal at top of parent window and focus input."""
        if self.parentWidget():
            parent_geom = self.parentWidget().geometry()
            x = parent_geom.x() + (parent_geom.width() - self.width()) // 2
            y = parent_geom.y() + 60
            self.move(x, y)

        self._search_input.clear()
        self._populate_list(self._indexed_files[:40])
        self.show()
        self.raise_()
        self.activateWindow()
        self._search_input.setFocus()

    def _populate_list(self, rel_paths: Sequence[str]) -> None:
        self._list_widget.clear()
        for path in rel_paths:
            basename = os.path.basename(path)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, basename)
            item.setData(Qt.ItemDataRole.UserRole + 1, path)
            self._list_widget.addItem(item)

        if self._list_widget.count() > 0:
            self._list_widget.setCurrentRow(0)

    def _on_query_changed(self, text: str) -> None:
        clean_text, line_number = FuzzyMatcher.parse_query_line(text)
        self._target_line = line_number if line_number is not None else 1

        if not clean_text:
            self._populate_list(self._indexed_files[:40])
            return

        ranked = FuzzyMatcher.filter_and_rank(clean_text, self._indexed_files, limit=50)
        matched_paths = [r[0] for r in ranked]
        self._populate_list(matched_paths)

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        rel_path = str(item.data(Qt.ItemDataRole.UserRole + 1))
        abs_path = self._abs_map.get(rel_path, "")
        if abs_path and os.path.exists(abs_path):
            self.file_selected.emit(abs_path, self._target_line)
        self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
            return
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            row = self._list_widget.currentRow()
            delta = 1 if key == Qt.Key.Key_Down else -1
            new_row = max(0, min(self._list_widget.count() - 1, row + delta))
            self._list_widget.setCurrentRow(new_row)
            return
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            current_item = self._list_widget.currentItem()
            if current_item is not None:
                self._on_item_activated(current_item)
            return
        super().keyPressEvent(event)


# =============================================================================
# 3. Universal Command Palette (Ctrl+Shift+P)
# =============================================================================


@dataclass
class PaletteAction:
    """Registered executable action in the Universal Command Palette."""

    title: str
    category: str
    shortcut: str = ""
    trigger_callback: Callable[[], None] | None = None
    action_ref: QAction | None = field(default=None)

    @property
    def display_string(self) -> str:
        return f"{self.category}: {self.title}"


class CommandPaletteDelegate(QStyledItemDelegate):
    """Custom renderer for Command Palette items showing Category, Title, and Shortcut."""

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: object
    ) -> None:
        painter.save()
        rect = option.rect  # type: ignore[attr-defined]

        is_selected = option.state & QStyleOptionViewItem.StateFlag.State_Selected  # type: ignore[attr-defined]
        if is_selected:
            painter.fillRect(rect, option.palette.highlight())  # type: ignore[attr-defined]
        elif option.state & QStyleOptionViewItem.StateFlag.State_MouseOver:  # type: ignore[attr-defined]
            painter.fillRect(rect, QColor(255, 255, 255, 12))

        category = str(index.data(Qt.ItemDataRole.UserRole) or "")  # type: ignore[attr-defined]
        title = str(index.data(Qt.ItemDataRole.UserRole + 1) or "")  # type: ignore[attr-defined]
        shortcut = str(index.data(Qt.ItemDataRole.UserRole + 2) or "")  # type: ignore[attr-defined]

        font = option.font  # type: ignore[attr-defined]
        metrics = QFontMetrics(font)

        # Draw Category Pill
        cat_color = QColor("#58a6ff") if not is_selected else option.palette.highlightedText().color()  # type: ignore[attr-defined]
        painter.setPen(cat_color)
        cat_text = f"[{category}] "
        painter.drawText(rect.x() + 10, rect.y() + metrics.ascent() + 6, cat_text)
        cat_width = metrics.horizontalAdvance(cat_text)

        # Draw Title
        title_color = option.palette.highlightedText().color() if is_selected else option.palette.text().color()  # type: ignore[attr-defined]
        painter.setPen(title_color)
        painter.drawText(rect.x() + 10 + cat_width, rect.y() + metrics.ascent() + 6, title)

        # Draw Shortcut aligned right
        if shortcut:
            shortcut_color = QColor(139, 148, 158) if not is_selected else title_color
            painter.setPen(shortcut_color)
            sc_width = metrics.horizontalAdvance(shortcut)
            painter.drawText(rect.right() - sc_width - 12, rect.y() + metrics.ascent() + 6, shortcut)

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: object) -> QSize:
        return QSize(option.rect.width() if hasattr(option, "rect") else 400, 32)  # type: ignore[attr-defined]


class CommandPaletteDialog(QDialog):
    """Universal Command Launcher spotlight modal (Ctrl+Shift+P)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedWidth(640)
        self.setFixedHeight(380)

        self._actions: list[PaletteAction] = []
        self._init_ui()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        frame = QFrame(self)
        frame.setObjectName("commandPaletteFrame")
        frame.setStyleSheet(
            "#commandPaletteFrame {"
            "  background-color: #161b22;"
            "  border: 1px solid #30363d;"
            "  border-radius: 6px;"
            "}"
        )
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(6, 6, 6, 6)
        frame_layout.setSpacing(6)

        self._search_input = QLineEdit(frame)
        self._search_input.setPlaceholderText("> Type a command or action name...")
        self._search_input.setStyleSheet(
            "QLineEdit {"
            "  background-color: #0d1117;"
            "  color: #c9d1d9;"
            "  border: 1px solid #238636;"
            "  border-radius: 4px;"
            "  padding: 8px 12px;"
            "  font-size: 13px;"
            "}"
        )
        self._search_input.textChanged.connect(self._on_query_changed)
        frame_layout.addWidget(self._search_input)

        self._list_widget = QListWidget(frame)
        self._list_widget.setItemDelegate(CommandPaletteDelegate(self))
        self._list_widget.setStyleSheet(
            "QListWidget {"
            "  background-color: #0d1117;"
            "  border: 1px solid #21262d;"
            "  border-radius: 4px;"
            "}"
            "QListWidget::item {"
            "  height: 32px;"
            "}"
        )
        self._list_widget.itemDoubleClicked.connect(self._on_item_activated)
        frame_layout.addWidget(self._list_widget)

        main_layout.addWidget(frame)

    def register_action(
        self,
        title: str,
        category: str,
        shortcut: str = "",
        callback: Callable[[], None] | None = None,
        action_ref: QAction | None = None,
    ) -> None:
        """Register an action to be searchable and executable via the palette."""
        clean_title = title.replace("&", "")
        self._actions.append(
            PaletteAction(
                title=clean_title,
                category=category,
                shortcut=shortcut,
                trigger_callback=callback,
                action_ref=action_ref,
            )
        )

    def register_qactions_from_menu(self, menu_title: str, actions: Sequence[QAction]) -> None:
        """Batch index actions directly from a QMenu."""
        for act in actions:
            if act.isSeparator() or not act.text():
                continue
            sc_str = act.shortcut().toString() if not act.shortcut().isEmpty() else ""
            self.register_action(
                title=act.text(),
                category=menu_title.replace("&", ""),
                shortcut=sc_str,
                action_ref=act,
            )

    def open_dialog(self) -> None:
        """Center and display the command palette."""
        if self.parentWidget():
            parent_geom = self.parentWidget().geometry()
            x = parent_geom.x() + (parent_geom.width() - self.width()) // 2
            y = parent_geom.y() + 60
            self.move(x, y)

        self._search_input.clear()
        self._populate_list(self._actions)
        self.show()
        self.raise_()
        self.activateWindow()
        self._search_input.setFocus()

    def _populate_list(self, actions: Sequence[PaletteAction]) -> None:
        self._list_widget.clear()
        for act in actions:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, act.category)
            item.setData(Qt.ItemDataRole.UserRole + 1, act.title)
            item.setData(Qt.ItemDataRole.UserRole + 2, act.shortcut)
            item.setData(Qt.ItemDataRole.UserRole + 3, act)
            self._list_widget.addItem(item)

        if self._list_widget.count() > 0:
            self._list_widget.setCurrentRow(0)

    def _on_query_changed(self, text: str) -> None:
        clean = text.strip()
        if not clean:
            self._populate_list(self._actions)
            return

        display_strings = [a.display_string for a in self._actions]
        ranked = FuzzyMatcher.filter_and_rank(clean, display_strings, limit=40)
        action_map = {a.display_string: a for a in self._actions}

        matched_actions = [action_map[r[0]] for r in ranked if r[0] in action_map]
        self._populate_list(matched_actions)

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        action_obj = item.data(Qt.ItemDataRole.UserRole + 3)
        self.close()
        if isinstance(action_obj, PaletteAction):
            if action_obj.action_ref is not None:
                action_obj.action_ref.trigger()
            elif action_obj.trigger_callback is not None:
                action_obj.trigger_callback()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
            return
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            row = self._list_widget.currentRow()
            delta = 1 if key == Qt.Key.Key_Down else -1
            new_row = max(0, min(self._list_widget.count() - 1, row + delta))
            self._list_widget.setCurrentRow(new_row)
            return
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            current_item = self._list_widget.currentItem()
            if current_item is not None:
                self._on_item_activated(current_item)
            return
        super().keyPressEvent(event)


# =============================================================================
# 4. Multi-Threaded Search in Files (Ctrl+Shift+H)
# =============================================================================


@dataclass
class SearchMatch:
    """A single matching occurrence inside a workspace file."""

    file_path: str
    line_number: int
    column_number: int
    line_text: str
    match_length: int


class SearchInFilesWorker(QThread):
    """Background worker scanning workspace files asynchronously."""

    match_found = Signal(object)  # SearchMatch
    search_finished = Signal(int, int, float)  # total_matches, total_files_with_matches, elapsed_sec
    search_error = Signal(str)

    def __init__(
        self,
        root_dir: Path,
        query: str,
        case_sensitive: bool = False,
        whole_word: bool = False,
        use_regex: bool = False,
        includes: str = "",
        excludes: str = "",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.root_dir = root_dir
        self.query = query
        self.case_sensitive = case_sensitive
        self.whole_word = whole_word
        self.use_regex = use_regex
        self.includes = [p.strip() for p in includes.split(",") if p.strip()]
        self.excludes = [p.strip() for p in excludes.split(",") if p.strip()]
        self._is_stopped = False

    def stop(self) -> None:
        self._is_stopped = True

    def run(self) -> None:
        start_time = time.perf_counter()
        if not self.query or not self.root_dir.exists():
            self.search_finished.emit(0, 0, 0.0)
            return

        try:
            # Build regex pattern
            pattern_str = self.query
            if not self.use_regex:
                pattern_str = re.escape(pattern_str)
            if self.whole_word:
                pattern_str = rf"\b{pattern_str}\b"

            flags = 0 if self.case_sensitive else re.IGNORECASE
            regex = re.compile(pattern_str, flags)
        except re.error as exc:
            self.search_error.emit(f"Invalid Regular Expression: {exc}")
            return

        total_matches = 0
        matched_files_count = 0

        for root, dirs, files in os.walk(self.root_dir):
            if self._is_stopped:
                break

            dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORED_DIRS and not d.startswith(".")]

            for filename in files:
                if self._is_stopped:
                    break

                ext = os.path.splitext(filename)[1].lower()
                if ext in DEFAULT_IGNORED_EXTENSIONS:
                    continue

                abs_path = os.path.join(root, filename)

                # Check include globs
                if self.includes and not any(fnmatch.fnmatch(filename, pat) for pat in self.includes):
                    continue

                # Check exclude globs
                if self.excludes and any(fnmatch.fnmatch(filename, pat) for pat in self.excludes):
                    continue

                file_has_match = False
                try:
                    with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                        for line_idx, line in enumerate(f, 1):
                            if self._is_stopped:
                                break

                            for match in regex.finditer(line):
                                col = match.start()
                                m_len = match.end() - match.start()
                                match_obj = SearchMatch(
                                    file_path=abs_path,
                                    line_number=line_idx,
                                    column_number=col,
                                    line_text=line.rstrip("\r\n"),
                                    match_length=m_len,
                                )
                                self.match_found.emit(match_obj)
                                total_matches += 1
                                file_has_match = True

                except (OSError, UnicodeDecodeError):
                    continue

                if file_has_match:
                    matched_files_count += 1

        elapsed = time.perf_counter() - start_time
        self.search_finished.emit(total_matches, matched_files_count, elapsed)


class SearchInFilesWidget(QWidget):
    """Panel tab for searching across all files in the active workspace."""

    match_selected = Signal(str, int, int)  # (file_path, line, col)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace_root: Path | None = None
        self._worker: SearchInFilesWorker | None = None
        self._file_tree_items: dict[str, QTreeWidgetItem] = {}

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Control Bar
        control_layout = QHBoxLayout()
        control_layout.setSpacing(6)

        self._search_input = QLineEdit(self)
        self._search_input.setPlaceholderText("Search in files (Press Enter to run)...")
        self._search_input.returnPressed.connect(self.start_search)
        control_layout.addWidget(self._search_input, stretch=3)

        self._case_check = QCheckBox("Match Case", self)
        self._case_check.setToolTip("Case Sensitive")
        control_layout.addWidget(self._case_check)

        self._word_check = QCheckBox("Whole Word", self)
        self._word_check.setToolTip("Match Whole Word")
        control_layout.addWidget(self._word_check)

        self._regex_check = QCheckBox("Regex", self)
        self._regex_check.setToolTip("Use Regular Expressions")
        control_layout.addWidget(self._regex_check)

        self._btn_search = QPushButton("Search", self)
        self._btn_search.clicked.connect(self.start_search)
        control_layout.addWidget(self._btn_search)

        self._btn_stop = QPushButton("Stop", self)
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self.stop_search)
        control_layout.addWidget(self._btn_stop)

        layout.addLayout(control_layout)

        # Filter Bar (Includes / Excludes)
        filter_layout = QHBoxLayout()
        filter_layout.setSpacing(6)

        self._include_input = QLineEdit(self)
        self._include_input.setPlaceholderText("files to include (e.g. *.py, src/*)")
        self._include_input.returnPressed.connect(self.start_search)
        filter_layout.addWidget(self._include_input)

        self._exclude_input = QLineEdit(self)
        self._exclude_input.setPlaceholderText("files to exclude (e.g. tests/*)")
        self._exclude_input.returnPressed.connect(self.start_search)
        filter_layout.addWidget(self._exclude_input)

        layout.addLayout(filter_layout)

        # Results Tree
        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels(["File / Code Match", "Line", "Col"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self._tree)

        # Status Bar
        self._status_label = QLabel("Ready to search.", self)
        self._status_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        layout.addWidget(self._status_label)

    def set_workspace_root(self, root: Path | None) -> None:
        self._workspace_root = root

    def focus_search(self) -> None:
        self._search_input.setFocus()
        self._search_input.selectAll()

    @Slot()
    def start_search(self) -> None:
        query = self._search_input.text().strip()
        if not query:
            self._status_label.setText("Enter a search term.")
            return

        if not self._workspace_root or not self._workspace_root.exists():
            self._status_label.setText("No active workspace directory.")
            return

        self.stop_search()

        self._tree.clear()
        self._file_tree_items.clear()
        self._status_label.setText(f"Searching for '{query}'...")
        self._btn_search.setEnabled(False)
        self._btn_stop.setEnabled(True)

        self._worker = SearchInFilesWorker(
            root_dir=self._workspace_root,
            query=query,
            case_sensitive=self._case_check.isChecked(),
            whole_word=self._word_check.isChecked(),
            use_regex=self._regex_check.isChecked(),
            includes=self._include_input.text().strip(),
            excludes=self._exclude_input.text().strip(),
        )
        self._worker.match_found.connect(self._on_match_found)
        self._worker.search_finished.connect(self._on_search_finished)
        self._worker.search_error.connect(self._on_search_error)
        self._worker.start()

    @Slot()
    def stop_search(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(500)
            self._worker = None
        self._btn_search.setEnabled(True)
        self._btn_stop.setEnabled(False)

    @Slot(object)
    def _on_match_found(self, match: SearchMatch) -> None:
        file_path = match.file_path
        if file_path not in self._file_tree_items:
            rel_name = (
                os.path.relpath(file_path, self._workspace_root)
                if self._workspace_root
                else os.path.basename(file_path)
            )
            file_item = QTreeWidgetItem(self._tree, [f"📄 {rel_name}", "", ""])
            file_item.setData(0, Qt.ItemDataRole.UserRole, file_path)
            file_item.setExpanded(True)
            self._file_tree_items[file_path] = file_item
        else:
            file_item = self._file_tree_items[file_path]

        line_item = QTreeWidgetItem(
            file_item,
            [match.line_text.strip(), str(match.line_number), str(match.column_number + 1)],
        )
        line_item.setData(0, Qt.ItemDataRole.UserRole, file_path)
        line_item.setData(1, Qt.ItemDataRole.UserRole, match.line_number)
        line_item.setData(2, Qt.ItemDataRole.UserRole, match.column_number)

    @Slot(int, int, float)
    def _on_search_finished(self, total_matches: int, total_files: int, elapsed_sec: float) -> None:
        self._btn_search.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._status_label.setText(
            f"Found {total_matches} matches in {total_files} files ({elapsed_sec:.2f}s)"
        )

    @Slot(str)
    def _on_search_error(self, message: str) -> None:
        self._btn_search.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._status_label.setText(f"Search Error: {message}")

    @Slot(QTreeWidgetItem, int)
    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        file_path = item.data(0, Qt.ItemDataRole.UserRole)
        line = item.data(1, Qt.ItemDataRole.UserRole)
        col = item.data(2, Qt.ItemDataRole.UserRole)
        if file_path and line is not None:
            self.match_selected.emit(str(file_path), int(line), int(col or 0))
