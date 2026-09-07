"""Unit tests for CPython internals, bytecode disassembler, AST inspector,
symtable analyzer, execution profiler, and InternalsPanel widget.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.internals import (
    AstInspectionResult,
    AstInspector,
    BytecodeDisassembler,
    DisassemblyInstruction,
    DisassemblyResult,
    ExecutionProfiler,
    ProfileResult,
    SymtableInspector,
    SymtableResult,
)
from src.panels import InternalsPanel
from src.pip_viper import EditorTheme, get_palette


@pytest.fixture(scope="session")
def qapp():
    """Ensure QApplication instance exists for GUI tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# =============================================================================
# BytecodeDisassembler Tests
# =============================================================================


def test_disassemble_empty_source():
    result = BytecodeDisassembler.disassemble("")
    assert len(result.instructions) == 0
    assert result.error is None
    assert result.total_instructions == 0


def test_disassemble_simple_arithmetic():
    source = "x = 10\ny = 20\nz = x + y\n"
    result = BytecodeDisassembler.disassemble(source)
    assert result.error is None
    assert result.total_instructions > 0
    assert result.code_object_count >= 1

    opnames = [instr.opname for instr in result.instructions]
    assert any("STORE" in op for op in opnames)
    assert any("LOAD" in op for op in opnames)


def test_disassemble_nested_functions_and_jumps():
    source = """
def calculate(value: int) -> int:
    if value > 0:
        return value * 2
    else:
        return 0

result = calculate(5)
"""
    result = BytecodeDisassembler.disassemble(source)
    assert result.error is None
    assert result.code_object_count >= 2  # module + calculate function

    # Check jump targets
    has_jump = any(instr.is_jump_target for instr in result.instructions)
    assert has_jump

    # Check line numbers exist
    lines = [instr.line for instr in result.instructions if instr.line is not None]
    assert len(lines) > 0


def test_disassemble_syntax_error():
    source = "def broken(:\n    pass\n"
    result = BytecodeDisassembler.disassemble(source)
    assert result.error is not None
    assert "SyntaxError" in result.error
    assert len(result.instructions) == 0


# =============================================================================
# AstInspector Tests
# =============================================================================


def test_ast_inspect_empty():
    result = AstInspector.inspect("")
    assert result.root is None
    assert result.total_nodes == 0
    assert result.syntax_valid is True


def test_ast_inspect_structure():
    source = """
import os

class Greeter:
    def __init__(self, greeting: str = "Hello"):
        self.greeting = greeting

    def greet(self, name: str) -> str:
        '''Return formatted greeting.'''
        return f"{self.greeting}, {name}!"
"""
    result = AstInspector.inspect(source)
    assert result.syntax_valid is True
    assert result.error is None
    assert result.total_nodes > 10
    assert result.root is not None
    assert result.root.node_type == "Module"

    # Find ClassDef and FunctionDef nodes
    types_found = set()

    def walk_info(node):
        types_found.add(node.node_type)
        for child in node.children:
            walk_info(child)

    walk_info(result.root)
    assert "ClassDef" in types_found
    assert "FunctionDef" in types_found
    assert "Import" in types_found
    assert "Return" in types_found


def test_ast_inspect_syntax_error():
    source = "def foo(x\n    return x\n"
    result = AstInspector.inspect(source)
    assert result.syntax_valid is False
    assert result.error is not None
    assert "SyntaxError" in result.error


# =============================================================================
# SymtableInspector Tests
# =============================================================================


def test_symtable_inspect_empty():
    result = SymtableInspector.inspect("")
    assert result.root_scope is None
    assert result.total_scopes == 0
    assert result.error is None


def test_symtable_inspect_closures_and_scopes():
    source = """
global_var = 100

def outer_func(param1):
    closure_var = 42
    def inner_func(param2):
        return param1 + closure_var + param2
    return inner_func
"""
    result = SymtableInspector.inspect(source)
    assert result.error is None
    assert result.root_scope is not None
    assert result.total_scopes >= 3  # top, outer_func, inner_func

    root = result.root_scope
    assert root.scope_type == "module"
    symbol_names = [s.name for s in root.symbols]
    assert "global_var" in symbol_names
    assert "outer_func" in symbol_names

    # Check outer_func scope
    outer = root.children[0]
    assert outer.name == "outer_func"
    assert outer.scope_type == "function"
    outer_syms = {s.name: s for s in outer.symbols}
    assert "param1" in outer_syms
    assert outer_syms["param1"].is_parameter is True

    # Check inner_func scope
    inner = outer.children[0]
    assert inner.name == "inner_func"
    assert inner.scope_type == "function"
    inner_syms = {s.name: s for s in inner.symbols}
    assert "param2" in inner_syms
    assert inner_syms["param2"].is_parameter is True
    # In inner_func, param1 and closure_var are free variables (from enclosing scope)
    assert inner_syms["closure_var"].is_free is True
    assert inner_syms["param1"].is_free is True


def test_symtable_inspect_syntax_error():
    source = "def (bad):\n"
    result = SymtableInspector.inspect(source)
    assert result.error is not None
    assert "SyntaxError" in result.error


# =============================================================================
# ExecutionProfiler Tests
# =============================================================================


def test_execution_profiler_empty():
    result = ExecutionProfiler.profile_code("")
    assert len(result.records) == 0
    assert result.error is None


def test_execution_profiler_computations():
    source = """
def compute_sum(n):
    total = 0
    for i in range(n):
        total += i
    return total

print("Sum computed:", compute_sum(500))
"""
    result = ExecutionProfiler.profile_code(source)
    assert result.error is None
    assert "Sum computed: 124750" in result.stdout
    assert result.peak_memory_bytes > 0

    record_funcs = [r.function_name for r in result.records]
    assert "compute_sum" in record_funcs

    comp_rec = next(r for r in result.records if r.function_name == "compute_sum")
    assert comp_rec.call_count == 1
    assert comp_rec.total_time_sec >= 0.0


def test_execution_profiler_runtime_exception():
    source = """
def will_fail():
    return 1 / 0

will_fail()
"""
    result = ExecutionProfiler.profile_code(source)
    assert result.error is not None
    assert "ZeroDivisionError" in result.error


def test_execution_profiler_syntax_error():
    source = "invalid syntax %%"
    result = ExecutionProfiler.profile_code(source)
    assert result.error is not None
    assert "SyntaxError" in result.error


# =============================================================================
# InternalsPanel Widget Tests
# =============================================================================


def test_internals_panel_lifecycle(qapp):
    palette = get_palette(EditorTheme.DARK)
    panel = InternalsPanel(palette)

    # 1. Test Bytecode Population
    source = "def add(a, b):\n    return a + b\n"
    dis_res = BytecodeDisassembler.disassemble(source)
    panel.populate_bytecode(dis_res)
    assert panel._bytecode_table.rowCount() == len(dis_res.instructions)

    # Test line highlighting
    panel.highlight_bytecode_for_line(1)
    selected_rows = panel._bytecode_table.selectionModel().selectedRows()
    assert len(selected_rows) > 0

    # 2. Test AST Population
    ast_res = AstInspector.inspect(source)
    panel.populate_ast(ast_res)
    assert panel._ast_tree.topLevelItemCount() == 1

    # 3. Test Symtable Population
    sym_res = SymtableInspector.inspect(source)
    panel.populate_symtable(sym_res)
    assert panel._scopes_tree.topLevelItemCount() == 1
    panel._on_scope_selected()
    assert panel._symbols_table.rowCount() > 0

    # 4. Test Profiler Population
    prof_res = ExecutionProfiler.profile_code(source)
    panel.populate_profile(prof_res)
    assert panel._profile_table.rowCount() == len(prof_res.records)

    # 5. Test Clear Profiler
    panel.clear_profiler()
    assert panel._profile_table.rowCount() == 0
    assert panel._memory_table.rowCount() == 0

    # 6. Test theme refresh
    light_palette = get_palette(EditorTheme.LIGHT)
    panel.set_palette(light_palette)
    assert panel._palette == light_palette


def test_internals_panel_signals(qapp):
    palette = get_palette(EditorTheme.DARK)
    panel = InternalsPanel(palette)

    # Verify line_activated signal on double click bytecode
    received_lines = []
    panel.line_activated.connect(received_lines.append)

    dis_res = BytecodeDisassembler.disassemble("x = 10\ny = 20\n")
    panel.populate_bytecode(dis_res)
    item = panel._bytecode_table.item(0, 0)
    assert item is not None
    panel._on_bytecode_double_clicked(item)
    assert len(received_lines) == 1
    assert received_lines[0] in (1, 2)

    # Verify node_activated signal on double click AST
    received_nodes = []
    panel.node_activated.connect(lambda l, c: received_nodes.append((l, c)))

    ast_res = AstInspector.inspect("x = 10\n")
    panel.populate_ast(ast_res)
    root_item = panel._ast_tree.topLevelItem(0)
    if root_item and root_item.childCount() > 0:
        child_item = root_item.child(0)
        panel._on_ast_item_double_clicked(child_item, 0)
        assert len(received_nodes) == 1
        assert received_nodes[0][0] == 1
