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
# CPython Internals Panel (Bytecode, AST, Symtable, Profiler)
# -----------------------------------------------------------------------------


class InternalsPanel(QWidget):
    """Deep CPython inspection workspace providing synchronized views of:
    1. Bytecode Disassembly (dis.Bytecode instructions, offsets, jumps)
    2. AST Syntax Tree (interactive hierarchical node explorer)
    3. Symtable Scopes (closures, free variables, and cell variables)
    4. Execution & Memory Profiler (cProfile duration and tracemalloc allocations)
    """

    line_activated = Signal(int)  # 1-based source code line
    node_activated = Signal(int, int)  # line, col (1-based line, 0-based col)
    file_and_line_activated = Signal(str, int)  # file_path, line
    profile_requested = Signal()

    def __init__(self, palette: ColorPalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette: ColorPalette = palette
        self._line_to_bytecode_rows: dict[int, list[int]] = {}
        self._scope_map: dict[str, ScopeInfo] = {}

        self._tabs = QTabWidget(self)

        # Tab 1: Bytecode Disassembly
        self._bytecode_table = QTableWidget(0, 7, self)
        self._bytecode_table.setHorizontalHeaderLabels(
            ["Offset", "Jump", "Opcode", "Arg", "Arg Value / Repr", "Line", "Scope"]
        )
        self._bytecode_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self._bytecode_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._bytecode_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._bytecode_table.setColumnWidth(0, 70)
        self._bytecode_table.setColumnWidth(1, 55)
        self._bytecode_table.setColumnWidth(2, 170)
        self._bytecode_table.setColumnWidth(3, 60)
        self._bytecode_table.setColumnWidth(5, 60)
        self._bytecode_table.setColumnWidth(6, 120)
        self._bytecode_table.itemDoubleClicked.connect(self._on_bytecode_double_clicked)

        self._bytecode_status = QLabel("Bytecode: 0 instructions", self)
        self._bytecode_status.setStyleSheet(f"color: {palette.muted}; font-weight: bold;")
        bytecode_layout = QVBoxLayout()
        bytecode_layout.setContentsMargins(6, 6, 6, 6)
        bytecode_layout.addWidget(self._bytecode_status)
        bytecode_layout.addWidget(self._bytecode_table)
        bytecode_container = QWidget(self)
        bytecode_container.setLayout(bytecode_layout)

        # Tab 2: AST Syntax Tree
        self._ast_tree = QTreeWidget(self)
        self._ast_tree.setHeaderLabels(
            ["Syntax Node", "Identifier / Value", "Line:Col Range", "Attributes / Details"]
        )
        self._ast_tree.header().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self._ast_tree.setColumnWidth(0, 220)
        self._ast_tree.setColumnWidth(1, 180)
        self._ast_tree.setColumnWidth(2, 130)
        self._ast_tree.itemDoubleClicked.connect(self._on_ast_item_double_clicked)

        self._ast_status = QLabel("AST: 0 nodes", self)
        self._ast_status.setStyleSheet(f"color: {palette.muted}; font-weight: bold;")
        ast_layout = QVBoxLayout()
        ast_layout.setContentsMargins(6, 6, 6, 6)
        ast_layout.addWidget(self._ast_status)
        ast_layout.addWidget(self._ast_tree)
        ast_container = QWidget(self)
        ast_container.setLayout(ast_layout)

        # Tab 3: Symtable Scopes
        self._scopes_tree = QTreeWidget(self)
        self._scopes_tree.setHeaderLabels(["Lexical Scope", "Type", "Line"])
        self._scopes_tree.setColumnWidth(0, 180)
        self._scopes_tree.setColumnWidth(1, 90)
        self._scopes_tree.itemSelectionChanged.connect(self._on_scope_selected)

        self._symbols_table = QTableWidget(0, 8, self)
        self._symbols_table.setHorizontalHeaderLabels(
            ["Symbol", "Local", "Global", "Param", "Free", "Cell", "Assigned", "Imported"]
        )
        self._symbols_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._symbols_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._symbols_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        for col_idx in range(1, 8):
            self._symbols_table.setColumnWidth(col_idx, 65)

        sym_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        sym_splitter.addWidget(self._scopes_tree)
        sym_splitter.addWidget(self._symbols_table)
        sym_splitter.setStretchFactor(0, 1)
        sym_splitter.setStretchFactor(1, 2)

        sym_layout = QVBoxLayout()
        sym_layout.setContentsMargins(6, 6, 6, 6)
        sym_layout.addWidget(sym_splitter)
        sym_container = QWidget(self)
        sym_container.setLayout(sym_layout)

        # Tab 4: Execution & Memory Profiler
        self._profile_btn = QPushButton("⚡ Profile Active Code", self)
        self._profile_btn.setProperty("role", "primary")
        self._profile_btn.clicked.connect(self.profile_requested)

        self._clear_profile_btn = QPushButton("🧹 Clear", self)
        self._clear_profile_btn.clicked.connect(self.clear_profiler)

        self._profile_status = QLabel("Profiler: IDLE", self)
        self._profile_status.setStyleSheet(f"color: {palette.muted}; font-weight: bold;")

        self._peak_memory_label = QLabel("Peak Memory: 0 KB", self)
        self._peak_memory_label.setStyleSheet(f"color: {palette.blue}; font-weight: bold;")

        prof_toolbar = QHBoxLayout()
        prof_toolbar.addWidget(self._profile_btn)
        prof_toolbar.addWidget(self._clear_profile_btn)
        prof_toolbar.addWidget(self._profile_status)
        prof_toolbar.addStretch(1)
        prof_toolbar.addWidget(self._peak_memory_label)

        self._prof_subtabs = QTabWidget(self)

        # Sub-tab 1: Flame Graph
        self._flame_graph = FlameGraphWidget(palette, self)
        self._flame_graph.navigate_requested.connect(self._on_navigate_requested)
        self._prof_subtabs.addTab(self._flame_graph, "🔥 Flame Graph")

        # Sub-tab 2: Call Hierarchy
        self._call_hierarchy = CallHierarchyView(palette, self)
        self._call_hierarchy.navigate_requested.connect(self._on_navigate_requested)
        self._prof_subtabs.addTab(self._call_hierarchy, "🌳 Call Hierarchy")

        # Sub-tab 3: Hotspots Table
        self._profile_table = QTableWidget(0, 7, self)
        self._profile_table.setHorizontalHeaderLabels(
            ["Function", "File", "Line", "Calls", "Total Time (s)", "Cumulative (s)", "Per Call (s)"]
        )
        self._profile_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._profile_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._profile_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._profile_table.itemDoubleClicked.connect(self._on_profile_item_double_clicked)

        self._memory_table = QTableWidget(0, 4, self)
        self._memory_table.setHorizontalHeaderLabels(
            ["File", "Line", "Size (bytes)", "Allocs"]
        )
        self._memory_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._memory_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._memory_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._memory_table.itemDoubleClicked.connect(self._on_memory_item_double_clicked)

        prof_box_top = QGroupBox("⏱ Function Execution Times (cProfile)", self)
        prof_top_layout = QVBoxLayout(prof_box_top)
        prof_top_layout.addWidget(self._profile_table)

        mem_box = QGroupBox("💾 Memory Hotspots (tracemalloc)", self)
        mem_layout = QVBoxLayout(mem_box)
        mem_layout.addWidget(self._memory_table)

        prof_hotspots_splitter = QSplitter(Qt.Orientation.Vertical, self)
        prof_hotspots_splitter.addWidget(prof_box_top)
        prof_hotspots_splitter.addWidget(mem_box)
        prof_hotspots_splitter.setStretchFactor(0, 2)
        prof_hotspots_splitter.setStretchFactor(1, 1)
        self._prof_subtabs.addTab(prof_hotspots_splitter, "⚡ Hotspots Table")

        # Sub-tab 4: Memory Diagnostics & Leaks
        self._memory_tracker = MemoryTrackerWidget(palette, self)
        self._memory_tracker.navigate_requested.connect(self._on_navigate_requested)
        self._prof_subtabs.addTab(self._memory_tracker, "💾 Memory & Leaks")

        # Sub-tab 5: Output
        self._prof_output = QPlainTextEdit(self)
        self._prof_output.setReadOnly(True)
        self._prof_output.setFont(QFont("Consolas", 10))
        self._prof_output.setPlaceholderText("Execution output will appear here...")
        self._prof_subtabs.addTab(self._prof_output, "📃 Output")

        prof_layout = QVBoxLayout()
        prof_layout.setContentsMargins(6, 6, 6, 6)
        prof_layout.addLayout(prof_toolbar)
        prof_layout.addWidget(self._prof_subtabs)
        prof_container = QWidget(self)
        prof_container.setLayout(prof_layout)

        # Assemble main sub-tabs
        self._tabs.addTab(bytecode_container, "⚙ Bytecode (dis)")
        self._tabs.addTab(ast_container, "🌳 Syntax Tree (AST)")
        self._tabs.addTab(sym_container, "🔍 Scopes (symtable)")
        self._tabs.addTab(prof_container, "⏱ Profiler (cProfile)")

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(self._tabs)

        self.set_palette(palette)

    def set_palette(self, palette: ColorPalette) -> None:
        """Update colors dynamically across tables, trees, and views."""
        self._palette = palette
        self._prof_output.setStyleSheet(
            f"background-color: {palette.background}; color: {palette.text};"
        )
        self._bytecode_table.setStyleSheet(
            f"background-color: {palette.background}; color: {palette.text};"
        )
        self._ast_tree.setStyleSheet(
            f"background-color: {palette.background}; color: {palette.text};"
        )
        self._scopes_tree.setStyleSheet(
            f"background-color: {palette.background}; color: {palette.text};"
        )
        self._symbols_table.setStyleSheet(
            f"background-color: {palette.background}; color: {palette.text};"
        )
        self._profile_table.setStyleSheet(
            f"background-color: {palette.background}; color: {palette.text};"
        )
        self._memory_table.setStyleSheet(
            f"background-color: {palette.background}; color: {palette.text};"
        )
        self._flame_graph.set_palette(palette)
        self._call_hierarchy.set_palette(palette)
        self._memory_tracker.set_palette(palette)

    # -------------------------------------------------------------------------
    # Bytecode Disassembly Population & Sync
    # -------------------------------------------------------------------------

    def populate_bytecode(self, result: DisassemblyResult) -> None:
        """Populate the bytecode table with instructions from dis."""
        self._line_to_bytecode_rows.clear()
        self._bytecode_table.setRowCount(0)

        if result.error:
            self._bytecode_status.setText(f"Bytecode: {result.error}")
            self._bytecode_status.setStyleSheet(f"color: {self._palette.red}; font-weight: bold;")
            return

        self._bytecode_status.setText(
            f"Bytecode: {result.total_instructions} instructions across {result.code_object_count} code object(s)"
        )
        self._bytecode_status.setStyleSheet(f"color: {self._palette.muted}; font-weight: bold;")

        self._bytecode_table.setRowCount(len(result.instructions))
        for row_idx, instr in enumerate(result.instructions):
            offset_item = QTableWidgetItem(str(instr.offset))
            jump_item = QTableWidgetItem(">>" if instr.is_jump_target else "")
            opcode_item = QTableWidgetItem(instr.opname)
            arg_item = QTableWidgetItem(str(instr.arg) if instr.arg is not None else "")
            argval_item = QTableWidgetItem(instr.argrepr or (str(instr.argval) if instr.argval is not None else ""))
            line_item = QTableWidgetItem(str(instr.line) if instr.line is not None else "")
            scope_item = QTableWidgetItem(instr.qualname)

            if instr.is_jump_target:
                jump_item.setForeground(_qcolor(self._palette.yellow))
                offset_item.setForeground(_qcolor(self._palette.yellow))

            if instr.opname.startswith("CALL") or instr.opname.startswith("LOAD_GLOBAL"):
                opcode_item.setForeground(_qcolor(self._palette.blue))
            elif instr.opname.startswith("RETURN"):
                opcode_item.setForeground(_qcolor(self._palette.green))
            elif instr.opname.startswith("JUMP"):
                opcode_item.setForeground(_qcolor(self._palette.orange))

            offset_item.setData(Qt.ItemDataRole.UserRole, instr.line)

            self._bytecode_table.setItem(row_idx, 0, offset_item)
            self._bytecode_table.setItem(row_idx, 1, jump_item)
            self._bytecode_table.setItem(row_idx, 2, opcode_item)
            self._bytecode_table.setItem(row_idx, 3, arg_item)
            self._bytecode_table.setItem(row_idx, 4, argval_item)
            self._bytecode_table.setItem(row_idx, 5, line_item)
            self._bytecode_table.setItem(row_idx, 6, scope_item)

            if instr.line is not None:
                self._line_to_bytecode_rows.setdefault(instr.line, []).append(row_idx)

    def highlight_bytecode_for_line(self, line: int) -> None:
        """Highlight table rows corresponding to the given editor source line."""
        rows = self._line_to_bytecode_rows.get(line, [])
        if not rows:
            return
        self._bytecode_table.clearSelection()
        model = self._bytecode_table.model()
        sel_model = self._bytecode_table.selectionModel()
        if not model or not sel_model:
            return
        for r in rows:
            idx = model.index(r, 0)
            sel_model.select(
                idx,
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )
        first_item = self._bytecode_table.item(rows[0], 0)
        if first_item:
            self._bytecode_table.scrollToItem(first_item)

    @Slot(QTableWidgetItem)
    def _on_bytecode_double_clicked(self, item: QTableWidgetItem) -> None:
        row_idx = item.row()
        target_item = self._bytecode_table.item(row_idx, 0)
        if target_item:
            line = target_item.data(Qt.ItemDataRole.UserRole)
            if isinstance(line, int) and line > 0:
                self.line_activated.emit(line)

    # -------------------------------------------------------------------------
    # AST Syntax Tree Population
    # -------------------------------------------------------------------------

    def populate_ast(self, result: AstInspectionResult) -> None:
        """Populate the AST tree widget with the parsed hierarchy."""
        self._ast_tree.clear()
        if result.error or not result.syntax_valid:
            self._ast_status.setText(f"AST: {result.error or 'Invalid syntax'}")
            self._ast_status.setStyleSheet(f"color: {self._palette.red}; font-weight: bold;")
            return

        self._ast_status.setText(f"AST: {result.total_nodes} nodes parsed successfully")
        self._ast_status.setStyleSheet(f"color: {self._palette.green}; font-weight: bold;")

        if result.root:
            root_item = self._create_ast_tree_item(result.root)
            self._ast_tree.addTopLevelItem(root_item)
            self._ast_tree.expandToDepth(2)

    def _create_ast_tree_item(self, node: AstNodeInfo) -> QTreeWidgetItem:
        span_str = ""
        if node.lineno is not None:
            span_str = f"L{node.lineno}:{node.col_offset or 0}"
            if node.end_lineno is not None:
                span_str += f" → L{node.end_lineno}:{node.end_col_offset or 0}"

        item = QTreeWidgetItem([node.node_type, node.name, span_str, node.detail])
        if node.lineno is not None:
            item.setData(0, Qt.ItemDataRole.UserRole, (node.lineno, node.col_offset or 0))

        # Colorize by category
        if "Def" in node.node_type:
            item.setForeground(0, _qcolor(self._palette.function_name))
        elif "Import" in node.node_type:
            item.setForeground(0, _qcolor(self._palette.purple))
        elif node.node_type in ("Assign", "AnnAssign", "AugAssign"):
            item.setForeground(0, _qcolor(self._palette.yellow))
        elif node.node_type in ("If", "For", "While", "Try", "With", "Match"):
            item.setForeground(0, _qcolor(self._palette.keyword))

        for child in node.children:
            item.addChild(self._create_ast_tree_item(child))

        return item

    @Slot(QTreeWidgetItem, int)
    def _on_ast_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and len(data) == 2:
            line, col = data
            self.node_activated.emit(int(line), int(col))

    # -------------------------------------------------------------------------
    # Symtable Scopes Population
    # -------------------------------------------------------------------------

    def populate_symtable(self, result: SymtableResult) -> None:
        """Populate the lexical scopes tree and symbol mapping."""
        self._scopes_tree.clear()
        self._symbols_table.setRowCount(0)
        self._scope_map.clear()

        if result.error or not result.root_scope:
            return

        root_item = self._create_scope_tree_item(result.root_scope)
        self._scopes_tree.addTopLevelItem(root_item)
        self._scopes_tree.expandAll()
        self._scopes_tree.setCurrentItem(root_item)
        root_item.setSelected(True)

    def _create_scope_tree_item(self, scope: ScopeInfo) -> QTreeWidgetItem:
        item_id = f"{scope.scope_type}:{scope.name}:{scope.lineno}"
        self._scope_map[item_id] = scope

        item = QTreeWidgetItem([scope.name, scope.scope_type, f"L{scope.lineno}"])
        item.setData(0, Qt.ItemDataRole.UserRole, item_id)

        if scope.scope_type == "function":
            item.setForeground(0, _qcolor(self._palette.function_name))
        elif scope.scope_type == "class":
            item.setForeground(0, _qcolor(self._palette.class_name))

        for child in scope.children:
            item.addChild(self._create_scope_tree_item(child))

        return item

    @Slot()
    def _on_scope_selected(self) -> None:
        selected_items = self._scopes_tree.selectedItems()
        target = selected_items[0] if selected_items else self._scopes_tree.currentItem()
        if not target:
            return
        item_id = target.data(0, Qt.ItemDataRole.UserRole)
        scope = self._scope_map.get(item_id)
        if not scope:
            return

        self._symbols_table.setRowCount(len(scope.symbols))
        for row_idx, sym in enumerate(scope.symbols):
            name_item = QTableWidgetItem(sym.name)
            if sym.is_parameter:
                name_item.setForeground(_qcolor(self._palette.orange))
            elif sym.is_cell or sym.is_free:
                name_item.setForeground(_qcolor(self._palette.purple))

            self._symbols_table.setItem(row_idx, 0, name_item)
            self._symbols_table.setItem(row_idx, 1, QTableWidgetItem("✓" if sym.is_local else ""))
            self._symbols_table.setItem(row_idx, 2, QTableWidgetItem("✓" if sym.is_global else ""))
            self._symbols_table.setItem(row_idx, 3, QTableWidgetItem("✓" if sym.is_parameter else ""))
            self._symbols_table.setItem(row_idx, 4, QTableWidgetItem("✓" if sym.is_free else ""))
            self._symbols_table.setItem(row_idx, 5, QTableWidgetItem("✓" if sym.is_cell else ""))
            self._symbols_table.setItem(row_idx, 6, QTableWidgetItem("✓" if sym.is_assigned else ""))
            self._symbols_table.setItem(row_idx, 7, QTableWidgetItem("✓" if sym.is_imported else ""))

    # -------------------------------------------------------------------------
    # Profiler Population
    # -------------------------------------------------------------------------

    def set_profile_busy(self, busy: bool) -> None:
        """Update button state and status indicator during profile execution."""
        self._profile_btn.setEnabled(not busy)
        if busy:
            self._profile_status.setText("Profiler: RUNNING...")
            self._profile_status.setStyleSheet(f"color: {self._palette.yellow}; font-weight: bold;")
        else:
            self._profile_status.setText("Profiler: COMPLETED")
            self._profile_status.setStyleSheet(f"color: {self._palette.green}; font-weight: bold;")

    def populate_profile(self, result: ProfileResult) -> None:
        """Display execution profile times and tracemalloc memory allocations."""
        self.set_profile_busy(False)
        self._profile_table.setRowCount(len(result.records))
        for row_idx, rec in enumerate(result.records):
            func_item = QTableWidgetItem(rec.function_name)
            func_item.setData(Qt.ItemDataRole.UserRole, rec.line)
            file_item = QTableWidgetItem(rec.filename)
            line_item = QTableWidgetItem(str(rec.line))
            calls_item = QTableWidgetItem(str(rec.call_count))
            total_item = QTableWidgetItem(f"{rec.total_time_sec:.6f}")
            cum_item = QTableWidgetItem(f"{rec.cumulative_time_sec:.6f}")
            per_call_item = QTableWidgetItem(f"{rec.per_call_sec:.6f}")

            # Color hotspot functions taking noticeable cumulative time
            if rec.cumulative_time_sec > 0.05:
                cum_item.setForeground(_qcolor(self._palette.red))
            elif rec.cumulative_time_sec > 0.005:
                cum_item.setForeground(_qcolor(self._palette.yellow))

            self._profile_table.setItem(row_idx, 0, func_item)
            self._profile_table.setItem(row_idx, 1, file_item)
            self._profile_table.setItem(row_idx, 2, line_item)
            self._profile_table.setItem(row_idx, 3, calls_item)
            self._profile_table.setItem(row_idx, 4, total_item)
            self._profile_table.setItem(row_idx, 5, cum_item)
            self._profile_table.setItem(row_idx, 6, per_call_item)

        self._memory_table.setRowCount(len(result.memory_hotspots))
        for row_idx, mem in enumerate(result.memory_hotspots):
            file_item = QTableWidgetItem(mem.filename)
            file_item.setData(Qt.ItemDataRole.UserRole, mem.line)
            line_item = QTableWidgetItem(str(mem.line))
            size_item = QTableWidgetItem(f"{mem.size_bytes:,}")
            count_item = QTableWidgetItem(str(mem.count))

            self._memory_table.setItem(row_idx, 0, file_item)
            self._memory_table.setItem(row_idx, 1, line_item)
            self._memory_table.setItem(row_idx, 2, size_item)
            self._memory_table.setItem(row_idx, 3, count_item)

        # Populate visual flame graph and call hierarchy
        if result.root_call_node:
            self._flame_graph.load_call_node(result.root_call_node)
        if result.callers_map or result.callees_map:
            self._call_hierarchy.populate(result.callers_map, result.callees_map)

        peak_kb = result.peak_memory_bytes / 1024.0
        self._peak_memory_label.setText(
            f"Peak Memory: {peak_kb:.1f} KB | Duration: {result.total_duration_sec:.4f}s"
        )

        out_content = []
        if result.stdout:
            out_content.append(f"--- stdout ---\n{result.stdout}")
        if result.stderr:
            out_content.append(f"--- stderr ---\n{result.stderr}")
        if result.error:
            out_content.append(f"--- Runtime Exception ---\n{result.error}")
            self._profile_status.setText(f"Profiler: Error ({result.error.splitlines()[0]})")
            self._profile_status.setStyleSheet(f"color: {self._palette.red}; font-weight: bold;")

        self._prof_output.setPlainText("\n\n".join(out_content))

    def clear_profiler(self) -> None:
        """Reset profiler tables and outputs."""
        self._profile_table.setRowCount(0)
        self._memory_table.setRowCount(0)
        self._prof_output.clear()
        self._peak_memory_label.setText("Peak Memory: 0 KB")
        self._profile_status.setText("Profiler: IDLE")
        self._profile_status.setStyleSheet(f"color: {self._palette.muted}; font-weight: bold;")

    @Slot(str, int)
    def _on_navigate_requested(self, file_path: str, line: int) -> None:
        self.file_and_line_activated.emit(file_path, line)
        if line > 0:
            self.line_activated.emit(line)

    @Slot(QTableWidgetItem)
    def _on_profile_item_double_clicked(self, item: QTableWidgetItem) -> None:
        row_idx = item.row()
        func_item = self._profile_table.item(row_idx, 0)
        file_item = self._profile_table.item(row_idx, 1)
        if func_item:
            line = func_item.data(Qt.ItemDataRole.UserRole)
            file_name = file_item.text() if file_item else ""
            if isinstance(line, int) and line > 0:
                self.file_and_line_activated.emit(file_name, line)
                self.line_activated.emit(line)

    @Slot(QTableWidgetItem)
    def _on_memory_item_double_clicked(self, item: QTableWidgetItem) -> None:
        row_idx = item.row()
        file_item = self._memory_table.item(row_idx, 0)
        if file_item:
            line = file_item.data(Qt.ItemDataRole.UserRole)
            file_name = file_item.text()
            if isinstance(line, int) and line > 0:
                self.file_and_line_activated.emit(file_name, line)
                self.line_activated.emit(line)


