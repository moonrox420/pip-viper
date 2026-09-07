"""Unit tests for the memory diagnostics engine, snapshot comparator, and memory meter."""

from __future__ import annotations

import tracemalloc

import pytest

from src.memory_tracker import (
    MemoryDeltaRecord,
    MemorySnapshotComparator,
    MemoryTrackerWidget,
    ProcessMemoryInfo,
    ProcessMemorySampler,
)
from src.pip_viper import EditorTheme, get_palette
from src.widgets import MemoryMeterWidget
from typing import Generator


@pytest.fixture(autouse=True)
def cleanup_tracemalloc() -> Generator[None, None, None]:
    """Ensure tracemalloc is cleanly stopped after memory tests so it doesn't affect subsequent tests."""
    yield
    MemorySnapshotComparator.stop_tracing()


def test_process_memory_sampler() -> None:
    """Verify ProcessMemorySampler extracts realistic process memory metrics."""
    info = ProcessMemorySampler.get_memory_info()

    assert isinstance(info, ProcessMemoryInfo)
    assert info.working_set_bytes >= 0
    assert info.peak_working_set_bytes >= 0
    assert info.working_set_mb >= 0.0


def test_memory_snapshot_comparator() -> None:
    """Verify MemorySnapshotComparator calculates memory deltas between two states."""
    MemorySnapshotComparator.ensure_tracing()
    snap1 = MemorySnapshotComparator.take_snapshot()

    # Allocate memory to create a measurable diff
    retained_payload = [f"allocation_test_{i}" for i in range(25000)]

    snap2 = MemorySnapshotComparator.take_snapshot()
    diffs = MemorySnapshotComparator.compare_snapshots(snap1, snap2, max_records=25)

    assert len(diffs) > 0
    first = diffs[0]
    assert isinstance(first, MemoryDeltaRecord)
    assert first.filename != ""
    assert first.line > 0
    assert first.size_bytes >= 0

    # Ensure at least one line registered positive growth
    assert any(d.is_growth for d in diffs)


def test_memory_tracker_widget_lifecycle(qtbot: object) -> None:
    """Verify MemoryTrackerWidget baseline capture, comparison, and GC trigger."""
    palette = get_palette(EditorTheme.DARK)
    widget = MemoryTrackerWidget(palette)
    getattr(qtbot, "addWidget")(widget)

    assert widget._baseline_snapshot is None
    assert not widget._compare_btn.isEnabled()

    # Step 1: Baseline
    widget.take_baseline()
    assert widget._baseline_snapshot is not None
    assert widget._compare_btn.isEnabled()

    # Allocate objects
    _temp_data = [i * 3 for i in range(15000)]

    # Step 2: Compare & Diff
    widget.compare_current()
    assert widget._current_snapshot is not None
    assert widget._table.rowCount() > 0

    # Step 3: Force GC
    widget.force_gc()


def test_memory_meter_widget_lifecycle(qtbot: object) -> None:
    """Verify MemoryMeterWidget periodic polling and signal emission."""
    palette = get_palette(EditorTheme.DARK)
    meter = MemoryMeterWidget(palette, poll_interval_ms=1000)
    getattr(qtbot, "addWidget")(meter)

    assert meter._label.text().startswith("💾")
    assert "MB" in meter._label.text()
    assert "PipViper Memory" in meter.toolTip()

    # Test update trigger
    meter.update_memory()
    assert meter._label.text().startswith("💾")

    # Test clean timer stop on close
    meter.close()
    assert not meter._timer.isActive()
