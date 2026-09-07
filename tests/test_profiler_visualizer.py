"""Unit tests for the Flame Graph visualizer and Call Hierarchy analyzer."""

from __future__ import annotations

import cProfile
import pstats
from pathlib import Path

import pytest
from PySide6.QtCore import QPointF, Qt

from src.pip_viper import EditorTheme, get_palette
from src.profiler_visualizer import (
    CallHierarchyBuilder,
    CallHierarchyView,
    CallNode,
    CallerCalleeEntry,
    FlameGraphCanvas,
    FlameGraphWidget,
)


def _generate_test_pstats() -> pstats.Stats:
    """Generate reproducible pstats data from nested function executions."""
    def compute_sum(n: int) -> int:
        return sum(i * 2 for i in range(n))

    def worker_b() -> int:
        return compute_sum(500)

    def worker_a() -> int:
        total = 0
        for _ in range(5):
            total += worker_b()
        return total

    profiler = cProfile.Profile()
    profiler.enable()
    worker_a()
    profiler.disable()

    return pstats.Stats(profiler)


def test_call_node_properties() -> None:
    """Verify CallNode display names, self-time ratios, and depth tracking."""
    node = CallNode(
        id="worker_a@worker.py:12",
        function_name="worker_a",
        filename="src/worker.py",
        line=12,
        call_count=5,
        total_time_sec=0.01,
        cumulative_time_sec=0.05,
        percent_of_total=20.0,
        depth=1,
    )

    assert node.display_name == "worker_a (worker.py:12)"
    assert round(node.self_time_ratio, 2) == 0.20
    assert node.depth == 1


def test_call_hierarchy_builder_from_pstats() -> None:
    """Verify CallHierarchyBuilder creates an acyclic call tree from pstats."""
    stats = _generate_test_pstats()
    root, callers_map, callees_map = CallHierarchyBuilder.build_from_pstats(stats)

    assert root is not None
    assert root.cumulative_time_sec > 0
    assert len(root.children) > 0

    # Ensure caller and callee relationship mappings were populated
    assert len(callers_map) > 0
    assert len(callees_map) > 0

    # Verify at least one expected function exists in the tree
    all_names: set[str] = set()

    def collect_names(node: CallNode) -> None:
        all_names.add(node.function_name)
        for child in node.children:
            collect_names(child)

    collect_names(root)
    assert any("worker" in name or "compute" in name for name in all_names)


def test_flame_graph_canvas_layout(qtbot: object) -> None:
    """Verify FlameGraphCanvas computes rectangular frames and handles mouse selection."""
    palette = get_palette(EditorTheme.DARK)
    canvas = FlameGraphCanvas(palette)
    getattr(qtbot, "addWidget")(canvas)

    stats = _generate_test_pstats()
    root, _, _ = CallHierarchyBuilder.build_from_pstats(stats)
    canvas.set_root_node(root)

    # Frame rectangles should be computed
    assert len(canvas._rendered_frames) > 0
    first_frame = canvas._rendered_frames[0]
    assert first_frame.rect.width() > 0
    assert first_frame.rect.height() == canvas._frame_height

    # Test search query filtering
    canvas.set_search_query("worker")
    matching_frames = [f for f in canvas._rendered_frames if f.is_match]
    assert len(matching_frames) > 0


def test_flame_graph_widget_breadcrumbs_and_zoom(qtbot: object) -> None:
    """Verify FlameGraphWidget zoom navigation and breadcrumb updates."""
    palette = get_palette(EditorTheme.DARK)
    widget = FlameGraphWidget(palette)
    getattr(qtbot, "addWidget")(widget)

    stats = _generate_test_pstats()
    root, _, _ = CallHierarchyBuilder.build_from_pstats(stats)
    widget.load_call_node(root)

    assert len(widget._history) == 1
    assert widget._breadcrumbs_layout.count() >= 1

    # Simulate drill-down into child frame
    if root.children:
        child = root.children[0]
        widget._on_node_selected(child)
        assert len(widget._history) == 2
        assert widget._canvas._current_focus_node == child

        # Reset root
        widget._on_reset_root()
        assert len(widget._history) == 1
        assert widget._canvas._current_focus_node == root


def test_call_hierarchy_view_population(qtbot: object) -> None:
    """Verify CallHierarchyView populates callers and callees."""
    palette = get_palette(EditorTheme.DARK)
    view = CallHierarchyView(palette)
    getattr(qtbot, "addWidget")(view)

    stats = _generate_test_pstats()
    _, callers_map, callees_map = CallHierarchyBuilder.build_from_pstats(stats)
    view.populate(callers_map, callees_map)

    assert view._func_tree.topLevelItemCount() > 0
    current_item = view._func_tree.currentItem()
    assert current_item is not None
