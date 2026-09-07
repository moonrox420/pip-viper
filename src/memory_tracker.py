"""Memory diagnostics, snapshot comparator, and real-time process memory sampler.

Provides tracemalloc snapshot diffing, memory leak isolation, and lightweight
cross-platform process memory consumption monitoring with zero external dependencies.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import gc
import logging
import os
from pathlib import Path
import sys
import tracemalloc
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
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

from src.pip_viper import ThemePalette

_LOGGER = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Process Memory Sampler (Zero Dependencies)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessMemoryInfo:
    """Current process memory metrics captured from OS counters."""

    working_set_bytes: int
    peak_working_set_bytes: int
    private_bytes: int
    allocated_blocks: int

    @property
    def working_set_mb(self) -> float:
        return round(self.working_set_bytes / (1024 * 1024), 2)

    @property
    def peak_working_set_mb(self) -> float:
        return round(self.peak_working_set_bytes / (1024 * 1024), 2)

    @property
    def private_mb(self) -> float:
        return round(self.private_bytes / (1024 * 1024), 2)


class ProcessMemorySampler:
    """Queries current OS process memory counters safely across Windows and Unix."""

    _is_windows: bool = sys.platform == "win32"
    _psapi: Any = None
    _kernel32: Any = None
    _pmc_struct: Any = None

    @classmethod
    def _init_windows_psapi(cls) -> bool:
        if not cls._is_windows or cls._psapi is not None:
            return cls._psapi is not None

        try:
            import ctypes.wintypes

            class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.wintypes.DWORD),
                    ("PageFaultCount", ctypes.wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            psapi = ctypes.windll.psapi
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
                ctypes.wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL

            cls._psapi = psapi
            cls._kernel32 = kernel32
            cls._pmc_struct = PROCESS_MEMORY_COUNTERS_EX
            return True
        except Exception as e:
            _LOGGER.debug("Could not initialize Windows psapi: %s", e)
            return False

    @classmethod
    def get_memory_info(cls) -> ProcessMemoryInfo:
        """Sample current process memory usage."""
        allocated_blocks = getattr(sys, "getallocatedblocks", lambda: 0)()

        # 1. Windows GetProcessMemoryInfo fast-path
        if cls._is_windows and cls._init_windows_psapi():
            try:
                pmc = cls._pmc_struct()
                pmc.cb = ctypes.sizeof(cls._pmc_struct)
                handle = cls._kernel32.GetCurrentProcess()
                if cls._psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                    return ProcessMemoryInfo(
                        working_set_bytes=int(pmc.WorkingSetSize),
                        peak_working_set_bytes=int(pmc.PeakWorkingSetSize),
                        private_bytes=int(pmc.PrivateUsage),
                        allocated_blocks=allocated_blocks,
                    )
            except Exception as e:
                _LOGGER.debug("Windows psapi query failed: %s", e)

        # 2. Unix getrusage fallback
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF)
            # Linux: ru_maxrss in KB; macOS: ru_maxrss in bytes
            multiplier = 1 if sys.platform == "darwin" else 1024
            max_rss = usage.ru_maxrss * multiplier
            return ProcessMemoryInfo(
                working_set_bytes=max_rss,
                peak_working_set_bytes=max_rss,
                private_bytes=max_rss,
                allocated_blocks=allocated_blocks,
            )
        except Exception:
            pass

        # 3. Tracemalloc fallback
        try:
            if tracemalloc.is_tracing():
                current, peak = tracemalloc.get_traced_memory()
                return ProcessMemoryInfo(
                    working_set_bytes=current,
                    peak_working_set_bytes=peak,
                    private_bytes=current,
                    allocated_blocks=allocated_blocks,
                )
        except Exception:
            pass

        return ProcessMemoryInfo(
            working_set_bytes=0,
            peak_working_set_bytes=0,
            private_bytes=0,
            allocated_blocks=allocated_blocks,
        )


# -----------------------------------------------------------------------------
# Tracemalloc Snapshot Comparator
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryDeltaRecord:
    """Line-level memory allocation diff between two snapshots."""

    filename: str
    line: int
    size_bytes: int
    size_diff_bytes: int
    count: int
    count_diff: int
    traceback_lines: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_growth(self) -> bool:
        return self.size_diff_bytes > 0

    @property
    def is_leak_suspect(self) -> bool:
        """Flag lines that grew significantly in both bytes and allocation count."""
        return self.size_diff_bytes > 1024 and self.count_diff > 0


class MemorySnapshotComparator:
    """Captures and compares tracemalloc snapshots to diagnose memory growth and leaks."""

    @staticmethod
    def ensure_tracing(nframes: int = 5) -> None:
        if not tracemalloc.is_tracing():
            tracemalloc.start(nframes)

    @staticmethod
    def stop_tracing() -> None:
        if tracemalloc.is_tracing():
            tracemalloc.stop()

    @staticmethod
    def take_snapshot() -> tracemalloc.Snapshot:
        MemorySnapshotComparator.ensure_tracing()
        return tracemalloc.take_snapshot()

    @staticmethod
    def compare_snapshots(
        baseline: tracemalloc.Snapshot,
        current: tracemalloc.Snapshot,
        max_records: int = 50,
    ) -> list[MemoryDeltaRecord]:
        """Compute line-by-line delta between baseline and current snapshot."""
        diff_stats = current.compare_to(baseline, "lineno")
        results: list[MemoryDeltaRecord] = []

        for stat in diff_stats[:max_records]:
            frame = stat.traceback[0]
            tb_lines = tuple(str(f) for f in stat.traceback)

            results.append(
                MemoryDeltaRecord(
                    filename=Path(frame.filename).name,
                    line=frame.lineno,
                    size_bytes=stat.size,
                    size_diff_bytes=stat.size_diff,
                    count=stat.count,
                    count_diff=stat.count_diff,
                    traceback_lines=tb_lines,
                )
            )

        return results


# -----------------------------------------------------------------------------
# Memory Diagnostics & Leaks Widget
# -----------------------------------------------------------------------------


class MemoryTrackerWidget(QWidget):
    """Interactive memory diagnostics panel with snapshot diffing and traceback inspector."""

    navigate_requested = Signal(str, int)  # filename, line

    def __init__(self, palette: ThemePalette, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._baseline_snapshot: Optional[tracemalloc.Snapshot] = None
        self._current_snapshot: Optional[tracemalloc.Snapshot] = None
        self._delta_records: list[MemoryDeltaRecord] = []

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self._baseline_btn = QPushButton("📸 1. Take Baseline", self)
        self._baseline_btn.setToolTip("Record initial memory state before running code")
        self._baseline_btn.clicked.connect(self.take_baseline)
        toolbar.addWidget(self._baseline_btn)

        self._compare_btn = QPushButton("📊 2. Compare Snapshot & Diff", self)
        self._compare_btn.setToolTip("Compare current memory against the recorded baseline to find retained allocations")
        self._compare_btn.setEnabled(False)
        self._compare_btn.clicked.connect(self.compare_current)
        toolbar.addWidget(self._compare_btn)

        self._gc_btn = QPushButton("🧹 Collect Garbage", self)
        self._gc_btn.setToolTip("Trigger Python garbage collection (gc.collect())")
        self._gc_btn.clicked.connect(self.force_gc)
        toolbar.addWidget(self._gc_btn)

        self._status_label = QLabel("Ready • Click 'Take Baseline' to start tracking memory", self)
        self._status_label.setStyleSheet(f"color: {self._palette.muted}; font-weight: bold; font-size: 11px;")
        toolbar.addStretch(1)
        toolbar.addWidget(self._status_label)

        layout.addLayout(toolbar)

        # Main splitter: Table on top, Traceback drawer on bottom
        splitter = QSplitter(Qt.Orientation.Vertical, self)

        # Table
        self._table = QTableWidget(0, 6, self)
        self._table.setHorizontalHeaderLabels(
            ["File", "Line", "Size (bytes)", "Size Delta (Δ)", "Allocs", "Alloc Delta (Δ)"]
        )
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self._table.itemDoubleClicked.connect(self._on_item_double_clicked)
        splitter.addWidget(self._table)

        # Traceback Drawer
        tb_box = QFrame(self)
        tb_layout = QVBoxLayout(tb_box)
        tb_layout.setContentsMargins(0, 4, 0, 0)
        tb_title = QLabel("Allocation Call Stack Traceback", self)
        tb_title.setStyleSheet(f"color: {self._palette.blue}; font-weight: bold; font-size: 11px;")
        tb_layout.addWidget(tb_title)

        self._tb_text = QPlainTextEdit(self)
        self._tb_text.setReadOnly(True)
        self._tb_text.setFont(QFont("Consolas", 9))
        self._tb_text.setPlaceholderText("Select a memory record above to inspect its origin call stack...")
        tb_layout.addWidget(self._tb_text)

        splitter.addWidget(tb_box)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter)

    def set_palette(self, palette: ThemePalette) -> None:
        self._palette = palette
        self._status_label.setStyleSheet(f"color: {palette.muted}; font-weight: bold; font-size: 11px;")
        self._repopulate_table()

    def take_baseline(self) -> None:
        """Capture baseline snapshot."""
        self._baseline_snapshot = MemorySnapshotComparator.take_snapshot()
        mem = ProcessMemorySampler.get_memory_info()
        self._compare_btn.setEnabled(True)
        self._status_label.setText(
            f"Baseline Captured ({mem.working_set_mb} MB Working Set) • Run your operation and click 'Compare Snapshot'"
        )

    def compare_current(self) -> None:
        """Capture current snapshot and calculate delta against baseline."""
        if not self._baseline_snapshot:
            return

        self._current_snapshot = MemorySnapshotComparator.take_snapshot()
        self._delta_records = MemorySnapshotComparator.compare_snapshots(
            self._baseline_snapshot, self._current_snapshot
        )

        total_diff_bytes = sum(r.size_diff_bytes for r in self._delta_records)
        diff_kb = total_diff_bytes / 1024.0
        sign = "+" if total_diff_bytes >= 0 else ""

        self._status_label.setText(
            f"Comparison Complete: {sign}{diff_kb:.1f} KB net allocation shift ({len(self._delta_records)} hotspots)"
        )
        self._repopulate_table()

    def force_gc(self) -> None:
        """Force manual garbage collection run."""
        collected = gc.collect()
        mem = ProcessMemorySampler.get_memory_info()
        self._status_label.setText(f"🧹 Garbage collected {collected} unreachable objects • Current: {mem.working_set_mb} MB")

    def _repopulate_table(self) -> None:
        self._table.setRowCount(0)
        for row_idx, record in enumerate(self._delta_records):
            self._table.insertRow(row_idx)

            f_item = QTableWidgetItem(record.filename)
            l_item = QTableWidgetItem(str(record.line))
            sz_item = QTableWidgetItem(f"{record.size_bytes:,}")

            # Colorize diff
            diff_sign = "+" if record.size_diff_bytes > 0 else ""
            diff_str = f"{diff_sign}{record.size_diff_bytes:,}"
            diff_item = QTableWidgetItem(diff_str)

            c_item = QTableWidgetItem(str(record.count))
            c_diff_sign = "+" if record.count_diff > 0 else ""
            c_diff_item = QTableWidgetItem(f"{c_diff_sign}{record.count_diff}")

            if record.size_diff_bytes > 0:
                diff_item.setForeground(QColor(self._palette.red))
                c_diff_item.setForeground(QColor(self._palette.red))
            elif record.size_diff_bytes < 0:
                diff_item.setForeground(QColor(self._palette.green))
                c_diff_item.setForeground(QColor(self._palette.green))

            for col, item in enumerate([f_item, l_item, sz_item, diff_item, c_item, c_diff_item]):
                item.setData(Qt.ItemDataRole.UserRole, record)
                self._table.setItem(row_idx, col, item)

        if self._delta_records:
            self._table.selectRow(0)

    def _on_table_selection_changed(self) -> None:
        selected_items = self._table.selectedItems()
        if not selected_items:
            self._tb_text.clear()
            return

        record = selected_items[0].data(Qt.ItemDataRole.UserRole)
        if isinstance(record, MemoryDeltaRecord):
            if record.traceback_lines:
                formatted_tb = "\n".join(record.traceback_lines)
                self._tb_text.setPlainText(formatted_tb)
            else:
                self._tb_text.setPlainText(f"{record.filename}:{record.line} (No extended frames recorded)")

    def _on_item_double_clicked(self, item: QTableWidgetItem) -> None:
        record = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(record, MemoryDeltaRecord):
            if record.filename and record.line > 0:
                self.navigate_requested.emit(record.filename, record.line)
