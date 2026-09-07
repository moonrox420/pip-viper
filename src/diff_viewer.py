"""Visual Diff Viewer for inspecting Git changes in PipViper.

Provides unified and side-by-side colorized diff views, change statistics,
and 1-click stage, unstage, and discard operations.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .pip_viper import ColorPalette


class DiffLineType(str, Enum):
    """Classification of diff lines."""

    HEADER = "header"
    CONTEXT = "context"
    ADDITION = "addition"
    DELETION = "deletion"


@dataclass(frozen=True)
class DiffLine:
    """Represents a single parsed line in a diff comparison."""

    line_type: DiffLineType
    old_line_num: Optional[int]
    new_line_num: Optional[int]
    content: str


class DiffViewerWidget(QWidget):
    """Rich visual diff renderer with Unified and Side-by-Side modes."""

    stage_requested = Signal(Path)
    unstage_requested = Signal(Path)
    discard_requested = Signal(Path)

    def __init__(
        self,
        palette: ColorPalette,
        file_path: Optional[Path] = None,
        old_text: str = "",
        new_text: str = "",
        old_title: str = "HEAD",
        new_title: str = "Working Tree",
        is_staged: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._file_path = file_path or Path("diff")
        self._old_text = old_text
        self._new_text = new_text
        self._old_title = old_title
        self._new_title = new_title
        self._is_staged = is_staged
        self._side_by_side = False

        self._setup_ui()
        self._populate_diff()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Header toolbar
        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        self._title_label = QLabel(f"📄 {self._file_path.name}", header)
        self._title_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        header_layout.addWidget(self._title_label)

        self._stats_label = QLabel("", header)
        self._stats_label.setFont(QFont("Segoe UI", 9))
        header_layout.addWidget(self._stats_label)

        header_layout.addStretch(1)

        # Mode toggles (Unified vs Side-by-Side)
        self._btn_unified = QPushButton("Unified", header)
        self._btn_unified.setCheckable(True)
        self._btn_unified.setChecked(True)
        self._btn_side = QPushButton("Side-by-Side", header)
        self._btn_side.setCheckable(True)

        mode_group = QButtonGroup(self)
        mode_group.addButton(self._btn_unified)
        mode_group.addButton(self._btn_side)
        self._btn_unified.clicked.connect(lambda: self._set_mode(False))
        self._btn_side.clicked.connect(lambda: self._set_mode(True))

        header_layout.addWidget(self._btn_unified)
        header_layout.addWidget(self._btn_side)

        # Stage / Unstage / Discard actions
        if self._is_staged:
            self._action_btn = QPushButton("➖ Unstage", header)
            self._action_btn.clicked.connect(lambda: self.unstage_requested.emit(self._file_path))
        else:
            self._action_btn = QPushButton("➕ Stage", header)
            self._action_btn.clicked.connect(lambda: self.stage_requested.emit(self._file_path))
        header_layout.addWidget(self._action_btn)

        self._discard_btn = QPushButton("↺ Discard", header)
        self._discard_btn.clicked.connect(lambda: self.discard_requested.emit(self._file_path))
        header_layout.addWidget(self._discard_btn)

        layout.addWidget(header)

        # Unified Diff Table
        self._unified_table = QTableWidget(self)
        self._unified_table.setColumnCount(4)
        self._unified_table.setHorizontalHeaderLabels(["Old", "New", "±", "Content"])
        self._unified_table.verticalHeader().setVisible(False)
        self._unified_table.setShowGrid(False)
        self._unified_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._unified_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._unified_table.setFont(QFont("Consolas", 10))

        header_view = self._unified_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        # Side-by-Side View (Splitter with Left/Right tables)
        self._side_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._left_table = QTableWidget(self._side_splitter)
        self._left_table.setColumnCount(2)
        self._left_table.setHorizontalHeaderLabels(["Line", self._old_title])
        self._left_table.verticalHeader().setVisible(False)
        self._left_table.setShowGrid(False)
        self._left_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._left_table.setFont(QFont("Consolas", 10))
        self._left_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._left_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        self._right_table = QTableWidget(self._side_splitter)
        self._right_table.setColumnCount(2)
        self._right_table.setHorizontalHeaderLabels(["Line", self._new_title])
        self._right_table.verticalHeader().setVisible(False)
        self._right_table.setShowGrid(False)
        self._right_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._right_table.setFont(QFont("Consolas", 10))
        self._right_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._right_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        # Sync scrollbars
        self._left_table.verticalScrollBar().valueChanged.connect(
            self._right_table.verticalScrollBar().setValue
        )
        self._right_table.verticalScrollBar().valueChanged.connect(
            self._left_table.verticalScrollBar().setValue
        )

        self._side_splitter.addWidget(self._left_table)
        self._side_splitter.addWidget(self._right_table)
        self._side_splitter.setSizes([450, 450])
        self._side_splitter.hide()

        layout.addWidget(self._unified_table, 1)
        layout.addWidget(self._side_splitter, 1)

        self._apply_theme()

    def set_palette(self, palette: ColorPalette) -> None:
        """Update colors when theme switches."""
        self._palette = palette
        self._apply_theme()
        self._populate_diff()

    def _apply_theme(self) -> None:
        table_style = f"""
            QTableWidget {{
                background-color: {self._palette.background};
                color: {self._palette.text};
                border: 1px solid {self._palette.border};
                gridline-color: transparent;
            }}
            QHeaderView::section {{
                background-color: {self._palette.panel};
                color: {self._palette.muted};
                border: none;
                border-bottom: 1px solid {self._palette.border};
                padding: 4px 6px;
                font-weight: bold;
            }}
        """
        self._unified_table.setStyleSheet(table_style)
        self._left_table.setStyleSheet(table_style)
        self._right_table.setStyleSheet(table_style)

    def set_content(
        self,
        file_path: Path,
        old_text: str,
        new_text: str,
        is_staged: bool = False,
    ) -> None:
        """Update diff content and re-render."""
        self._file_path = file_path
        self._old_text = old_text
        self._new_text = new_text
        self._is_staged = is_staged
        self._title_label.setText(f"📄 {file_path.name}")
        if is_staged:
            self._action_btn.setText("➖ Unstage")
        else:
            self._action_btn.setText("➕ Stage")
        self._populate_diff()

    def _set_mode(self, side_by_side: bool) -> None:
        self._side_by_side = side_by_side
        if side_by_side:
            self._unified_table.hide()
            self._side_splitter.show()
        else:
            self._side_splitter.hide()
            self._unified_table.show()

    def _populate_diff(self) -> None:
        old_lines = self._old_text.splitlines()
        new_lines = self._new_text.splitlines()

        matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
        unified_lines: list[DiffLine] = []
        left_lines: list[tuple[Optional[int], str, DiffLineType]] = []
        right_lines: list[tuple[Optional[int], str, DiffLineType]] = []

        additions = 0
        deletions = 0

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for idx, line in enumerate(old_lines[i1:i2]):
                    old_num = i1 + idx + 1
                    new_num = j1 + idx + 1
                    unified_lines.append(DiffLine(DiffLineType.CONTEXT, old_num, new_num, f" {line}"))
                    left_lines.append((old_num, line, DiffLineType.CONTEXT))
                    right_lines.append((new_num, line, DiffLineType.CONTEXT))

            elif tag == "replace":
                del_count = i2 - i1
                add_count = j2 - j1
                deletions += del_count
                additions += add_count

                # Unified mode: show deletions first, then additions
                for idx, line in enumerate(old_lines[i1:i2]):
                    unified_lines.append(DiffLine(DiffLineType.DELETION, i1 + idx + 1, None, f"-{line}"))
                for idx, line in enumerate(new_lines[j1:j2]):
                    unified_lines.append(DiffLine(DiffLineType.ADDITION, None, j1 + idx + 1, f"+{line}"))

                # Side-by-side: align lines side by side
                max_lines = max(del_count, add_count)
                for idx in range(max_lines):
                    if idx < del_count:
                        left_lines.append((i1 + idx + 1, old_lines[i1 + idx], DiffLineType.DELETION))
                    else:
                        left_lines.append((None, "", DiffLineType.CONTEXT))

                    if idx < add_count:
                        right_lines.append((j1 + idx + 1, new_lines[j1 + idx], DiffLineType.ADDITION))
                    else:
                        right_lines.append((None, "", DiffLineType.CONTEXT))

            elif tag == "delete":
                del_count = i2 - i1
                deletions += del_count
                for idx, line in enumerate(old_lines[i1:i2]):
                    unified_lines.append(DiffLine(DiffLineType.DELETION, i1 + idx + 1, None, f"-{line}"))
                    left_lines.append((i1 + idx + 1, line, DiffLineType.DELETION))
                    right_lines.append((None, "", DiffLineType.CONTEXT))

            elif tag == "insert":
                add_count = j2 - j1
                additions += add_count
                for idx, line in enumerate(new_lines[j1:j2]):
                    unified_lines.append(DiffLine(DiffLineType.ADDITION, None, j1 + idx + 1, f"+{line}"))
                    left_lines.append((None, "", DiffLineType.CONTEXT))
                    right_lines.append((j1 + idx + 1, line, DiffLineType.ADDITION))

        # Update stats label
        self._stats_label.setText(
            f"<font color='{self._palette.green}'>+{additions}</font> "
            f"<font color='{self._palette.red}'>-{deletions}</font>"
        )

        # Colors
        green_bg = QColor(self._palette.green)
        green_bg.setAlpha(40)
        red_bg = QColor(self._palette.red)
        red_bg.setAlpha(40)
        muted_col = QColor(self._palette.muted)

        # Populate Unified table
        self._unified_table.setRowCount(len(unified_lines))
        for row, item in enumerate(unified_lines):
            old_item = QTableWidgetItem(str(item.old_line_num) if item.old_line_num else "")
            old_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            old_item.setForeground(muted_col)

            new_item = QTableWidgetItem(str(item.new_line_num) if item.new_line_num else "")
            new_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            new_item.setForeground(muted_col)

            indicator = item.content[0] if item.content else " "
            ind_item = QTableWidgetItem(indicator)
            ind_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            content_text = item.content[1:] if len(item.content) > 1 else ""
            content_item = QTableWidgetItem(content_text)

            bg_color = None
            if item.line_type == DiffLineType.ADDITION:
                bg_color = green_bg
                ind_item.setForeground(QColor(self._palette.green))
            elif item.line_type == DiffLineType.DELETION:
                bg_color = red_bg
                ind_item.setForeground(QColor(self._palette.red))

            if bg_color:
                old_item.setBackground(bg_color)
                new_item.setBackground(bg_color)
                ind_item.setBackground(bg_color)
                content_item.setBackground(bg_color)

            self._unified_table.setItem(row, 0, old_item)
            self._unified_table.setItem(row, 1, new_item)
            self._unified_table.setItem(row, 2, ind_item)
            self._unified_table.setItem(row, 3, content_item)

        # Populate Side-by-side tables
        total_side_rows = max(len(left_lines), len(right_lines))
        self._left_table.setRowCount(total_side_rows)
        self._right_table.setRowCount(total_side_rows)

        for row in range(total_side_rows):
            # Left side
            if row < len(left_lines):
                old_num, line_text, ltype = left_lines[row]
                l_num_item = QTableWidgetItem(str(old_num) if old_num else "")
                l_num_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                l_num_item.setForeground(muted_col)
                l_text_item = QTableWidgetItem(line_text)
                if ltype == DiffLineType.DELETION:
                    l_num_item.setBackground(red_bg)
                    l_text_item.setBackground(red_bg)
                self._left_table.setItem(row, 0, l_num_item)
                self._left_table.setItem(row, 1, l_text_item)

            # Right side
            if row < len(right_lines):
                new_num, line_text, rtype = right_lines[row]
                r_num_item = QTableWidgetItem(str(new_num) if new_num else "")
                r_num_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                r_num_item.setForeground(muted_col)
                r_text_item = QTableWidgetItem(line_text)
                if rtype == DiffLineType.ADDITION:
                    r_num_item.setBackground(green_bg)
                    r_text_item.setBackground(green_bg)
                self._right_table.setItem(row, 0, r_num_item)
                self._right_table.setItem(row, 1, r_text_item)


class DiffViewerDialog(QDialog):
    """Modal dialog displaying the DiffViewerWidget."""

    def __init__(
        self,
        palette: ColorPalette,
        file_path: Path,
        old_text: str,
        new_text: str,
        old_title: str = "HEAD",
        new_title: str = "Working Tree",
        is_staged: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Diff: {file_path.name}")
        self.resize(1000, 650)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._viewer = DiffViewerWidget(
            palette=palette,
            file_path=file_path,
            old_text=old_text,
            new_text=new_text,
            old_title=old_title,
            new_title=new_title,
            is_staged=is_staged,
            parent=self,
        )
        layout.addWidget(self._viewer, 1)

        bottom_bar = QHBoxLayout()
        bottom_bar.addStretch(1)
        close_btn = QPushButton("Close", self)
        close_btn.clicked.connect(self.accept)
        bottom_bar.addWidget(close_btn)
        layout.addLayout(bottom_bar)

        # Forward signals
        self.stage_requested = self._viewer.stage_requested
        self.unstage_requested = self._viewer.unstage_requested
        self.discard_requested = self._viewer.discard_requested


class DiffDialog(QDialog):
    """An interactive window showing side-by-side original and modified code for review."""

    def __init__(
        self,
        original_code: str,
        modified_code: str,
        palette: ColorPalette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review Code Adjustments")
        self.resize(1100, 750)

        self.setStyleSheet(
            f"QDialog {{ background-color: {palette.background}; color: {palette.text}; }}"
            f"QLabel {{ font-weight: bold; color: {palette.text}; }}"
            f"QPushButton {{ background-color: {palette.panel}; color: {palette.text}; border: 1px solid {palette.border}; border-radius: 3px; padding: 6px 12px; }}"
            f"QPushButton:hover {{ background-color: {palette.selection}; }}"
        )

        label_left = QLabel("Current Source Code:")
        label_right = QLabel("Refactored Code Adjustments:")

        self.original_view = QPlainTextEdit(self)
        self.original_view.setReadOnly(True)
        self.original_view.setFont(QFont("Consolas", 10))
        self.original_view.setPlainText(original_code)

        self.modified_view = QPlainTextEdit(self)
        self.modified_view.setFont(QFont("Consolas", 10))
        self.modified_view.setPlainText(modified_code)

        col_layout = QHBoxLayout()
        left_box = QVBoxLayout()
        left_box.addWidget(label_left)
        left_box.addWidget(self.original_view)

        right_box = QVBoxLayout()
        right_box.addWidget(label_right)
        right_box.addWidget(self.modified_view)

        col_layout.addLayout(left_box, 1)
        col_layout.addLayout(right_box, 1)

        self.accept_button = QPushButton("Accept and Update Code", self)
        self.accept_button.clicked.connect(self.accept)

        self.reject_button = QPushButton("Discard Suggestion", self)
        self.reject_button.clicked.connect(self.reject)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(self.reject_button)
        button_layout.addWidget(self.accept_button)

        layout = QVBoxLayout(self)
        layout.addLayout(col_layout)
        layout.addLayout(button_layout)

    def get_modified_code(self) -> str:
        """Return the user-reviewed code block."""
        return self.modified_view.toPlainText()

