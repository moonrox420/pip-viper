"""Interactive Flame Graph visualizer, call hierarchy analyzer, and hotspot ranking engine.

Provides visual call tree representations of Python execution profiles collected
via cProfile and pstats, enabling rapid hotspot identification and stack exploration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
from pathlib import Path
import pstats
from typing import Any, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal, Slot
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.pip_viper import EditorTheme, ThemePalette, get_palette

_LOGGER = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Call Tree Data Models
# -----------------------------------------------------------------------------


@dataclass
class CallNode:
    """A node in the hierarchical execution call tree."""

    id: str
    function_name: str
    filename: str
    line: int
    call_count: int
    total_time_sec: float  # self-time
    cumulative_time_sec: float  # inclusive time
    percent_of_total: float = 0.0
    depth: int = 0
    children: list[CallNode] = field(default_factory=list)
    parent: Optional[CallNode] = None

    @property
    def display_name(self) -> str:
        """Formatted name for visualization."""
        clean_file = Path(self.filename).name if self.filename else "<unknown>"
        return f"{self.function_name} ({clean_file}:{self.line})"

    @property
    def self_time_ratio(self) -> float:
        """Ratio of self-time to cumulative time (0.0 to 1.0)."""
        if self.cumulative_time_sec <= 0:
            return 0.0
        return min(1.0, max(0.0, self.total_time_sec / self.cumulative_time_sec))


@dataclass
class CallerCalleeEntry:
    """Caller or callee relationship entry."""

    function_name: str
    filename: str
    line: int
    call_count: int
    total_time_sec: float
    cumulative_time_sec: float
    percent_of_caller: float = 0.0


# -----------------------------------------------------------------------------
# Call Hierarchy Builder
# -----------------------------------------------------------------------------


class CallHierarchyBuilder:
    """Constructs structured call trees and caller/callee graphs from pstats.Stats."""

    @classmethod
    def build_from_pstats(cls, stats: pstats.Stats) -> tuple[CallNode, dict[str, list[CallerCalleeEntry]], dict[str, list[CallerCalleeEntry]]]:
        """Convert a pstats.Stats instance into an acyclic CallNode tree.

        Returns:
            Tuple of (root_call_node, callers_map, callees_map).
        """
        stats.calc_callees()
        raw_stats = stats.stats  # (file, line, name) -> (cc, nc, tt, ct, callers)
        all_callees = getattr(stats, "all_callees", {})

        total_runtime = max(1e-9, getattr(stats, "total_tt", 0.0))

        # Build caller and callee relationship maps
        callers_map: dict[str, list[CallerCalleeEntry]] = {}
        callees_map: dict[str, list[CallerCalleeEntry]] = {}

        for (fn_file, fn_line, fn_name), (_cc, _nc, _tt, ct, callers) in raw_stats.items():
            key = f"{fn_name}@{Path(fn_file).name}:{fn_line}"
            callers_list: list[CallerCalleeEntry] = []
            for (c_file, c_line, c_name), (c_cc, c_nc, c_tt, c_ct) in callers.items():
                pct = (c_ct / ct * 100.0) if ct > 0 else 0.0
                callers_list.append(
                    CallerCalleeEntry(
                        function_name=c_name,
                        filename=Path(c_file).name,
                        line=c_line,
                        call_count=c_nc,
                        total_time_sec=round(c_tt, 6),
                        cumulative_time_sec=round(c_ct, 6),
                        percent_of_caller=round(pct, 1),
                    )
                )
            callers_map[key] = callers_list

        for (fn_file, fn_line, fn_name), callees in all_callees.items():
            key = f"{fn_name}@{Path(fn_file).name}:{fn_line}"
            callees_list: list[CallerCalleeEntry] = []
            parent_ct = raw_stats.get((fn_file, fn_line, fn_name), (0, 0, 0, 0))[3]
            for (c_file, c_line, c_name), (c_cc, c_nc, c_tt, c_ct) in callees.items():
                pct = (c_ct / parent_ct * 100.0) if parent_ct > 0 else 0.0
                callees_list.append(
                    CallerCalleeEntry(
                        function_name=c_name,
                        filename=Path(c_file).name,
                        line=c_line,
                        call_count=c_nc,
                        total_time_sec=round(c_tt, 6),
                        cumulative_time_sec=round(c_ct, 6),
                        percent_of_caller=round(pct, 1),
                    )
                )
            callees_map[key] = callees_list

        # Identify root candidates (no callers or <module>)
        root_candidates: list[tuple[str, int, str]] = []
        for key, (_cc, _nc, _tt, _ct, callers) in raw_stats.items():
            if not callers or key[2] == "<module>":
                root_candidates.append(key)

        if not root_candidates:
            # Fallback: Pick highest cumulative time node
            root_candidates = sorted(raw_stats.keys(), key=lambda k: raw_stats[k][3], reverse=True)[:1]

        # Create artificial root if multiple root entry points exist
        if len(root_candidates) == 1:
            prime_key = root_candidates[0]
            cc, nc, tt, ct, _ = raw_stats[prime_key]
            root = CallNode(
                id=f"{prime_key[2]}@{prime_key[0]}:{prime_key[1]}",
                function_name=prime_key[2],
                filename=prime_key[0],
                line=prime_key[1],
                call_count=nc,
                total_time_sec=round(tt, 6),
                cumulative_time_sec=round(ct, 6),
                percent_of_total=round((ct / total_runtime) * 100.0, 1),
                depth=0,
            )
            cls._populate_children(root, raw_stats, all_callees, total_runtime, set())
        else:
            root = CallNode(
                id="<all_threads>",
                function_name="<root>",
                filename="<system>",
                line=0,
                call_count=1,
                total_time_sec=0.0,
                cumulative_time_sec=round(total_runtime, 6),
                percent_of_total=100.0,
                depth=0,
            )
            for r_key in sorted(root_candidates, key=lambda k: raw_stats[k][3], reverse=True):
                r_cc, r_nc, r_tt, r_ct, _ = raw_stats[r_key]
                child = CallNode(
                    id=f"{r_key[2]}@{r_key[0]}:{r_key[1]}",
                    function_name=r_key[2],
                    filename=r_key[0],
                    line=r_key[1],
                    call_count=r_nc,
                    total_time_sec=round(r_tt, 6),
                    cumulative_time_sec=round(r_ct, 6),
                    percent_of_total=round((r_ct / total_runtime) * 100.0, 1),
                    depth=1,
                    parent=root,
                )
                cls._populate_children(child, raw_stats, all_callees, total_runtime, {r_key})
                root.children.append(child)

        return root, callers_map, callees_map

    @classmethod
    def _populate_children(
        cls,
        parent_node: CallNode,
        raw_stats: dict[Any, Any],
        all_callees: dict[Any, Any],
        total_runtime: float,
        visited_stack: set[tuple[str, int, str]],
        max_depth: int = 40,
    ) -> None:
        """Recursively assemble children frames while guarding against recursion cycles."""
        if parent_node.depth >= max_depth:
            return

        parent_key = (parent_node.filename, parent_node.line, parent_node.function_name)
        callees = all_callees.get(parent_key, {})

        # Sort callees by cumulative time descending
        sorted_callees = sorted(callees.items(), key=lambda item: item[1][3], reverse=True)

        for callee_key, (c_cc, c_nc, c_tt, c_ct) in sorted_callees:
            # Skip recursion cycles on current path
            if callee_key in visited_stack:
                continue

            # Skip internal cProfile/import frames
            if "cProfile.py" in callee_key[0] or "pstats.py" in callee_key[0]:
                continue

            child = CallNode(
                id=f"{callee_key[2]}@{callee_key[0]}:{callee_key[1]}",
                function_name=callee_key[2],
                filename=callee_key[0],
                line=callee_key[1],
                call_count=c_nc,
                total_time_sec=round(c_tt, 6),
                cumulative_time_sec=round(c_ct, 6),
                percent_of_total=round((c_ct / total_runtime) * 100.0, 1),
                depth=parent_node.depth + 1,
                parent=parent_node,
            )
            visited_stack.add(callee_key)
            cls._populate_children(
                child, raw_stats, all_callees, total_runtime, visited_stack, max_depth
            )
            visited_stack.remove(callee_key)
            parent_node.children.append(child)


# -----------------------------------------------------------------------------
# Flame Graph Canvas & Widget
# -----------------------------------------------------------------------------


@dataclass
class _RenderFrame:
    """Precomputed screen layout rectangle for a single CallNode."""

    node: CallNode
    rect: QRectF
    color: QColor
    is_match: bool


class FlameGraphCanvas(QWidget):
    """Interactive custom canvas rendering top-down Flame Chart frames."""

    node_selected = Signal(CallNode)
    node_double_clicked = Signal(str, int)  # filename, line
    status_changed = Signal(str)

    def __init__(self, palette: ThemePalette, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._root_node: Optional[CallNode] = None
        self._current_focus_node: Optional[CallNode] = None
        self._hovered_node: Optional[CallNode] = None
        self._search_query: str = ""
        self._rendered_frames: list[_RenderFrame] = []

        self._frame_height = 24.0
        self._frame_spacing = 2.0
        self._font = QFont("Consolas", 9)
        self.setMouseTracking(True)

    def set_palette(self, palette: ThemePalette) -> None:
        self._palette = palette
        self.update()

    def set_root_node(self, root: CallNode) -> None:
        self._root_node = root
        self._current_focus_node = root
        self._recompute_layout()
        self.update()

    def set_focus_node(self, node: CallNode) -> None:
        self._current_focus_node = node
        self._recompute_layout()
        self.update()

    def set_search_query(self, query: str) -> None:
        self._search_query = query.strip().lower()
        self._recompute_layout()
        self.update()

    def _get_node_color(self, node: CallNode) -> QColor:
        """Derive warm gradient color from self-time ratio and module hash."""
        # High self-time = hot (amber/coral/crimson), low self-time = cool/orange
        ratio = node.self_time_ratio
        # Stable hash from function name for slight distinct hue variation
        name_hash = int(hashlib.md5(node.function_name.encode()).hexdigest(), 16) % 30
        hue = int(15 + ratio * 35 + (name_hash - 15) * 0.4) % 360
        sat = int(180 + ratio * 60)
        val = int(180 + ratio * 50)
        return QColor.fromHsv(max(5, min(55, hue)), min(255, sat), min(255, val))

    def _recompute_layout(self) -> None:
        """Compute layout rectangles for active focus node and all descendants."""
        self._rendered_frames.clear()
        if not self._current_focus_node:
            return

        canvas_width = max(800.0, float(self.width() - 20))
        total_time = max(1e-9, self._current_focus_node.cumulative_time_sec)

        max_depth = 0

        def layout_node(node: CallNode, x: float, y: float, w: float, depth: int) -> None:
            nonlocal max_depth
            if depth > max_depth:
                max_depth = depth

            # Check search match
            is_match = False
            if self._search_query:
                is_match = (
                    self._search_query in node.function_name.lower()
                    or self._search_query in node.filename.lower()
                )

            rect = QRectF(x, y, max(1.0, w - self._frame_spacing), self._frame_height)
            color = self._get_node_color(node)
            self._rendered_frames.append(
                _RenderFrame(node=node, rect=rect, color=color, is_match=is_match)
            )

            # Layout children
            curr_x = x
            for child in node.children:
                child_w = (child.cumulative_time_sec / total_time) * canvas_width
                if child_w >= 1.0:
                    layout_node(
                        child,
                        curr_x,
                        y + self._frame_height + self._frame_spacing,
                        child_w,
                        depth + 1,
                    )
                curr_x += child_w

        layout_node(self._current_focus_node, 10.0, 10.0, canvas_width, 0)

        # Update canvas geometry height based on depth
        required_height = int((max_depth + 2) * (self._frame_height + self._frame_spacing) + 40)
        self.setMinimumHeight(max(300, required_height))
        self.setMinimumWidth(int(canvas_width + 20))

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._recompute_layout()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setFont(self._font)
        fm = QFontMetrics(self._font)

        # Clear background
        painter.fillRect(self.rect(), QColor(self._palette.surface))

        if not self._rendered_frames:
            painter.setPen(QColor(self._palette.muted))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "No profile data available. Run '⚡ Profile Active Code' to inspect execution frames.",
            )
            return

        for frame in self._rendered_frames:
            node = frame.node
            rect = frame.rect
            base_color = frame.color

            # Dim non-matches if a search query is active
            if self._search_query:
                if frame.is_match:
                    fill_color = QColor(self._palette.blue)
                    pen_color = QColor(self._palette.yellow)
                    border_width = 2
                else:
                    fill_color = QColor(base_color.red(), base_color.green(), base_color.blue(), 60)
                    pen_color = QColor(self._palette.border)
                    border_width = 1
            else:
                if node == self._hovered_node:
                    fill_color = base_color.lighter(130)
                    pen_color = QColor(self._palette.accent)
                    border_width = 2
                else:
                    fill_color = base_color
                    pen_color = base_color.darker(120)
                    border_width = 1

            painter.setPen(QPen(pen_color, border_width))
            painter.setBrush(QBrush(fill_color))
            painter.drawRect(rect)

            # Draw text label if wide enough
            if rect.width() >= 30:
                text_rect = rect.adjusted(4, 2, -4, -2)
                display_txt = f"{node.function_name} ({node.percent_of_total}%)"
                elided = fm.elidedText(display_txt, Qt.TextElideMode.ElideRight, int(text_rect.width()))
                painter.setPen(QColor("#ffffff" if fill_color.value() < 200 else "#1e1e1e"))
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        found: Optional[CallNode] = None
        for frame in self._rendered_frames:
            if frame.rect.contains(pos):
                found = frame.node
                break

        if found != self._hovered_node:
            self._hovered_node = found
            if found:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                info = (
                    f"🔥 {found.function_name} | {Path(found.filename).name}:{found.line} | "
                    f"Calls: {found.call_count} | Self: {found.total_time_sec:.6f}s ({found.self_time_ratio*100:.1f}%) | "
                    f"Cum: {found.cumulative_time_sec:.6f}s ({found.percent_of_total}%)"
                )
                self.status_changed.emit(info)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
                self.status_changed.emit("")
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            for frame in self._rendered_frames:
                if frame.rect.contains(pos):
                    self.node_selected.emit(frame.node)
                    break

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            for frame in self._rendered_frames:
                if frame.rect.contains(pos):
                    node = frame.node
                    if node.filename and node.line > 0:
                        self.node_double_clicked.emit(node.filename, node.line)
                    break


class FlameGraphWidget(QWidget):
    """Complete Flame Graph viewer widget with zoom breadcrumbs, search, and detail card."""

    navigate_requested = Signal(str, int)  # file_path, line

    def __init__(self, palette: ThemePalette, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._root_node: Optional[CallNode] = None
        self._history: list[CallNode] = []

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Header toolbar
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 4, 4, 4)

        self._reset_btn = QPushButton("🏠 Reset Root", self)
        self._reset_btn.setToolTip("Reset zoom back to top-level entry frame")
        self._reset_btn.clicked.connect(self._on_reset_root)
        toolbar.addWidget(self._reset_btn)

        self._breadcrumbs_layout = QHBoxLayout()
        self._breadcrumbs_layout.setContentsMargins(4, 0, 4, 0)
        toolbar.addLayout(self._breadcrumbs_layout)
        toolbar.addStretch(1)

        search_label = QLabel("🔍 Search:", self)
        search_label.setStyleSheet(f"color: {self._palette.muted}; font-size: 11px;")
        toolbar.addWidget(search_label)

        self._search_input = QLineEdit(self)
        self._search_input.setPlaceholderText("Filter functions...")
        self._search_input.setFixedWidth(180)
        self._search_input.textChanged.connect(self._on_search_changed)
        toolbar.addWidget(self._search_input)

        layout.addLayout(toolbar)

        # Scroll area for canvas
        self._canvas = FlameGraphCanvas(self._palette, self)
        self._canvas.node_selected.connect(self._on_node_selected)
        self._canvas.node_double_clicked.connect(self.navigate_requested)
        self._canvas.status_changed.connect(self._on_status_changed)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._canvas)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self._scroll, stretch=1)

        # Status footer
        self._status_bar = QLabel("Click any frame to zoom in • Double-click to open in editor", self)
        self._status_bar.setStyleSheet(
            f"background-color: {self._palette.panel}; color: {self._palette.muted}; "
            f"padding: 4px 8px; font-family: Consolas; font-size: 11px; border-top: 1px solid {self._palette.border};"
        )
        layout.addWidget(self._status_bar)

    def set_palette(self, palette: ThemePalette) -> None:
        self._palette = palette
        self._canvas.set_palette(palette)
        self._status_bar.setStyleSheet(
            f"background-color: {palette.panel}; color: {palette.muted}; "
            f"padding: 4px 8px; font-family: Consolas; font-size: 11px; border-top: 1px solid {palette.border};"
        )

    def load_call_node(self, root: CallNode) -> None:
        """Load root call tree into the visualizer."""
        self._root_node = root
        self._history = [root]
        self._canvas.set_root_node(root)
        self._update_breadcrumbs()

    def _on_node_selected(self, node: CallNode) -> None:
        """Drill down into node as new visual root."""
        if node == self._canvas._current_focus_node:
            return
        self._history.append(node)
        self._canvas.set_focus_node(node)
        self._update_breadcrumbs()

    def _on_reset_root(self) -> None:
        if self._root_node:
            self._history = [self._root_node]
            self._canvas.set_focus_node(self._root_node)
            self._update_breadcrumbs()

    def _on_search_changed(self, text: str) -> None:
        self._canvas.set_search_query(text)

    def _on_status_changed(self, text: str) -> None:
        if text:
            self._status_bar.setText(text)
        else:
            self._status_bar.setText("Click any frame to zoom in • Double-click to open in editor")

    def _update_breadcrumbs(self) -> None:
        """Rebuild clickable breadcrumb navigation bar."""
        # Clear existing buttons
        while self._breadcrumbs_layout.count():
            item = self._breadcrumbs_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for idx, node in enumerate(self._history):
            if idx > 0:
                sep = QLabel("›", self)
                sep.setStyleSheet(f"color: {self._palette.muted}; font-weight: bold;")
                self._breadcrumbs_layout.addWidget(sep)

            btn = QPushButton(node.function_name, self)
            btn.setStyleSheet(
                f"background: transparent; border: none; color: {self._palette.accent}; "
                f"font-weight: {'bold' if idx == len(self._history)-1 else 'normal'};"
            )
            # Capture node in closure
            btn.clicked.connect(lambda _chk=False, n=node, i=idx: self._jump_to_history(n, i))
            self._breadcrumbs_layout.addWidget(btn)

    def _jump_to_history(self, node: CallNode, index: int) -> None:
        self._history = self._history[: index + 1]
        self._canvas.set_focus_node(node)
        self._update_breadcrumbs()


# -----------------------------------------------------------------------------
# Call Hierarchy View (Callers & Callees)
# -----------------------------------------------------------------------------


class CallHierarchyView(QWidget):
    """Inspects hierarchical callers and callees for profiled functions."""

    navigate_requested = Signal(str, int)  # file_path, line

    def __init__(self, palette: ThemePalette, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._callers_map: dict[str, list[CallerCalleeEntry]] = {}
        self._callees_map: dict[str, list[CallerCalleeEntry]] = {}

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Left: Function List
        left_box = QFrame(self)
        left_layout = QVBoxLayout(left_box)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_title = QLabel("All Profiled Functions", self)
        left_title.setStyleSheet(f"color: {self._palette.muted}; font-weight: bold; font-size: 11px;")
        left_layout.addWidget(left_title)

        self._func_tree = QTreeWidget(self)
        self._func_tree.setHeaderLabels(["Function", "File:Line"])
        self._func_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._func_tree.currentItemChanged.connect(self._on_func_selected)
        self._func_tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        left_layout.addWidget(self._func_tree)
        splitter.addWidget(left_box)

        # Right: Split Callers and Callees
        right_box = QFrame(self)
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(0, 0, 0, 0)

        callers_title = QLabel("⬆ Callers (Called By)", self)
        callers_title.setStyleSheet(f"color: {self._palette.blue}; font-weight: bold; font-size: 11px;")
        right_layout.addWidget(callers_title)

        self._callers_tree = QTreeWidget(self)
        self._callers_tree.setHeaderLabels(["Caller", "Calls", "Total Time (s)", "Cumulative (s)", "% Share"])
        self._callers_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._callers_tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        right_layout.addWidget(self._callers_tree)

        callees_title = QLabel("⬇ Callees (Calls To)", self)
        callees_title.setStyleSheet(f"color: {self._palette.green}; font-weight: bold; font-size: 11px;")
        right_layout.addWidget(callees_title)

        self._callees_tree = QTreeWidget(self)
        self._callees_tree.setHeaderLabels(["Callee", "Calls", "Total Time (s)", "Cumulative (s)", "% Share"])
        self._callees_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._callees_tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        right_layout.addWidget(self._callees_tree)

        splitter.addWidget(right_box)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        layout.addWidget(splitter)

    def set_palette(self, palette: ThemePalette) -> None:
        self._palette = palette

    def populate(
        self,
        callers_map: dict[str, list[CallerCalleeEntry]],
        callees_map: dict[str, list[CallerCalleeEntry]],
    ) -> None:
        self._callers_map = callers_map
        self._callees_map = callees_map
        self._func_tree.clear()
        self._callers_tree.clear()
        self._callees_tree.clear()

        all_keys = sorted(set(list(callers_map.keys()) + list(callees_map.keys())))
        for key in all_keys:
            # key format: function_name@file:line
            parts = key.split("@")
            name = parts[0]
            loc = parts[1] if len(parts) > 1 else ""

            item = QTreeWidgetItem(self._func_tree)
            item.setText(0, name)
            item.setText(1, loc)
            item.setData(0, Qt.ItemDataRole.UserRole, key)

        if self._func_tree.topLevelItemCount() > 0:
            self._func_tree.setCurrentItem(self._func_tree.topLevelItem(0))

    def _on_func_selected(self, current: Optional[QTreeWidgetItem], _prev: Any) -> None:
        if not current:
            return
        key = current.data(0, Qt.ItemDataRole.UserRole)
        if not key:
            return

        self._callers_tree.clear()
        for caller in self._callers_map.get(key, []):
            item = QTreeWidgetItem(self._callers_tree)
            item.setText(0, f"{caller.function_name} ({caller.filename}:{caller.line})")
            item.setText(1, str(caller.call_count))
            item.setText(2, f"{caller.total_time_sec:.6f}")
            item.setText(3, f"{caller.cumulative_time_sec:.6f}")
            item.setText(4, f"{caller.percent_of_caller:.1f}%")
            item.setData(0, Qt.ItemDataRole.UserRole, (caller.filename, caller.line))

        self._callees_tree.clear()
        for callee in self._callees_map.get(key, []):
            item = QTreeWidgetItem(self._callees_tree)
            item.setText(0, f"{callee.function_name} ({callee.filename}:{callee.line})")
            item.setText(1, str(callee.call_count))
            item.setText(2, f"{callee.total_time_sec:.6f}")
            item.setText(3, f"{callee.cumulative_time_sec:.6f}")
            item.setText(4, f"{callee.percent_of_caller:.1f}%")
            item.setData(0, Qt.ItemDataRole.UserRole, (callee.filename, callee.line))

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and len(data) == 2:
            fn_file, fn_line = data
            if fn_file and fn_line > 0:
                self.navigate_requested.emit(fn_file, fn_line)
        elif isinstance(data, str) and "@" in data:
            loc = data.split("@")[1]
            if ":" in loc:
                fn_file, line_str = loc.split(":")
                try:
                    self.navigate_requested.emit(fn_file, int(line_str))
                except ValueError:
                    pass
