"""CPython internals, bytecode disassembler, AST inspector, and profiler engine.

This module provides deep introspection into Python runtime mechanics:
    * Bytecode disassembler using standard library `dis`
    * Hierarchical AST tree generator using standard library `ast`
    * Lexical scope and closure analyzer using standard library `symtable`
    * Execution duration and memory allocation tracking via `cProfile` and `tracemalloc`
"""

from __future__ import annotations

import ast
import cProfile
import dis
import io
import logging
import pstats
import symtable
import sys
import tracemalloc
import types
from pathlib import Path
from typing import Any, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .pip_viper import PipViperError
from .profiler_visualizer import CallHierarchyBuilder, CallNode

_LOGGER: logging.Logger = logging.getLogger("src.internals")

__all__ = [
    "InternalsError",
    "DisassemblyError",
    "AstInspectionError",
    "SymtableError",
    "ProfileExecutionError",
    "DisassemblyInstruction",
    "DisassemblyResult",
    "AstNodeInfo",
    "AstInspectionResult",
    "ScopeSymbol",
    "ScopeInfo",
    "SymtableResult",
    "ProfileRecord",
    "MemoryHotspot",
    "ProfileResult",
    "CallNode",
    "CallHierarchyBuilder",
    "BytecodeDisassembler",
    "AstInspector",
    "SymtableInspector",
    "ExecutionProfiler",
]


# -----------------------------------------------------------------------------
# Exception Hierarchy
# -----------------------------------------------------------------------------


class InternalsError(PipViperError):
    """Root exception for CPython internals operations."""


class DisassemblyError(InternalsError):
    """Bytecode disassembly failed due to compilation or execution error."""


class AstInspectionError(InternalsError):
    """AST parsing or traversal failed."""


class SymtableError(InternalsError):
    """Symtable lexical analysis failed."""


class ProfileExecutionError(InternalsError):
    """Code execution under the profiler failed."""


# -----------------------------------------------------------------------------
# Data Models
# -----------------------------------------------------------------------------


class DisassemblyInstruction(BaseModel):
    """A single disassembled bytecode instruction."""

    model_config = ConfigDict(frozen=True)

    offset: int = Field(ge=0)
    opname: str
    opcode: int = Field(ge=0)
    arg: int | None = None
    argval: Any = None
    argrepr: str = ""
    line: int | None = None
    is_jump_target: bool = False
    qualname: str = "<module>"


class DisassemblyResult(BaseModel):
    """Aggregate result of disassembling a code snippet or module."""

    model_config = ConfigDict(frozen=True)

    instructions: list[DisassemblyInstruction] = Field(default_factory=list)
    code_object_count: int = 0
    total_instructions: int = 0
    error: str | None = None


class AstNodeInfo(BaseModel):
    """Metadata and structural information for an AST node."""

    model_config = ConfigDict(frozen=True)

    node_type: str
    name: str = ""
    lineno: int | None = None
    end_lineno: int | None = None
    col_offset: int | None = None
    end_col_offset: int | None = None
    detail: str = ""
    children: list[AstNodeInfo] = Field(default_factory=list)


class AstInspectionResult(BaseModel):
    """Aggregate outcome of parsing and walking an Abstract Syntax Tree."""

    model_config = ConfigDict(frozen=True)

    root: AstNodeInfo | None = None
    total_nodes: int = 0
    syntax_valid: bool = True
    error: str | None = None


class ScopeSymbol(BaseModel):
    """A symbol defined or referenced within a lexical scope."""

    model_config = ConfigDict(frozen=True)

    name: str
    is_local: bool = False
    is_global: bool = False
    is_nonlocal: bool = False
    is_parameter: bool = False
    is_free: bool = False
    is_cell: bool = False
    is_assigned: bool = False
    is_imported: bool = False


class ScopeInfo(BaseModel):
    """A single lexical scope (module, class, function) and its symbols."""

    model_config = ConfigDict(frozen=True)

    name: str
    scope_type: str  # "module", "function", "class"
    lineno: int = 1
    is_nested: bool = False
    symbols: list[ScopeSymbol] = Field(default_factory=list)
    children: list[ScopeInfo] = Field(default_factory=list)


class SymtableResult(BaseModel):
    """Aggregate outcome of analyzing symbol tables and closures."""

    model_config = ConfigDict(frozen=True)

    root_scope: ScopeInfo | None = None
    total_scopes: int = 0
    error: str | None = None


class ProfileRecord(BaseModel):
    """Function-level performance statistics captured by cProfile."""

    model_config = ConfigDict(frozen=True)

    function_name: str
    filename: str
    line: int
    call_count: int
    total_time_sec: float
    cumulative_time_sec: float
    per_call_sec: float


class MemoryHotspot(BaseModel):
    """Line-level memory allocation captured by tracemalloc."""

    model_config = ConfigDict(frozen=True)

    filename: str
    line: int
    size_bytes: int
    count: int


class ProfileResult(BaseModel):
    """Aggregate outcome of profiling execution time and memory allocations."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    records: list[ProfileRecord] = Field(default_factory=list)
    memory_hotspots: list[MemoryHotspot] = Field(default_factory=list)
    peak_memory_bytes: int = 0
    total_duration_sec: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    root_call_node: Optional[CallNode] = None
    callers_map: dict[str, list[Any]] = Field(default_factory=dict)
    callees_map: dict[str, list[Any]] = Field(default_factory=dict)


ProfileResult.model_rebuild()


# -----------------------------------------------------------------------------
# Bytecode Disassembler Engine
# -----------------------------------------------------------------------------


class BytecodeDisassembler:
    """Compiles and disassembles Python code into structured opcode streams."""

    @classmethod
    def disassemble(
        cls,
        source: str,
        filename: str = "<editor>",
    ) -> DisassemblyResult:
        """Compile source code and disassemble all code objects recursively.

        Args:
            source: Raw Python source code.
            filename: Virtual or physical file name for compiling.

        Returns:
            DisassemblyResult containing all disassembled instructions.
        """
        if not source.strip():
            return DisassemblyResult()

        try:
            compiled_code = compile(source, filename, "exec")
        except SyntaxError as syntax_error:
            _LOGGER.debug("Disassembly compilation failed: %s", syntax_error)
            return DisassemblyResult(
                error=f"SyntaxError at line {syntax_error.lineno}: {syntax_error.msg}"
            )
        except Exception as general_error:
            _LOGGER.error("Compilation error: %s", general_error, exc_info=True)
            return DisassemblyResult(error=f"Compilation error: {general_error}")

        all_instructions: list[DisassemblyInstruction] = []
        code_objects: list[tuple[str, types.CodeType]] = [("<module>", compiled_code)]
        visited_codes: set[int] = {id(compiled_code)}

        # Traverse nested code objects (functions, classes, comprehensions, generators)
        i = 0
        while i < len(code_objects):
            qualname, code_obj = code_objects[i]
            i += 1
            for const_value in code_obj.co_consts:
                if isinstance(const_value, types.CodeType) and id(const_value) not in visited_codes:
                    visited_codes.add(id(const_value))
                    child_name = f"{qualname}.{const_value.co_name}" if qualname != "<module>" else const_value.co_name
                    code_objects.append((child_name, const_value))

        for qualname, code_obj in code_objects:
            try:
                current_line: int | None = None
                for instr in dis.get_instructions(code_obj):
                    if instr.starts_line is not None:
                        current_line = max(1, instr.starts_line)

                    argval_str = str(instr.argval) if instr.argval is not None else ""
                    # Keep argval concise if it's a huge string or code object repr
                    if len(argval_str) > 100:
                        argval_str = argval_str[:97] + "..."

                    all_instructions.append(
                        DisassemblyInstruction(
                            offset=instr.offset,
                            opname=instr.opname,
                            opcode=instr.opcode,
                            arg=instr.arg,
                            argval=argval_str if instr.argval is not None else None,
                            argrepr=instr.argrepr or "",
                            line=current_line,
                            is_jump_target=instr.is_jump_target,
                            qualname=qualname,
                        )
                    )
            except Exception as dis_err:
                _LOGGER.warning("Failed disassembling %s: %s", qualname, dis_err)

        return DisassemblyResult(
            instructions=all_instructions,
            code_object_count=len(code_objects),
            total_instructions=len(all_instructions),
        )


# -----------------------------------------------------------------------------
# AST Inspector Engine
# -----------------------------------------------------------------------------


class AstInspector:
    """Parses and traverses an Abstract Syntax Tree into a hierarchical model."""

    @classmethod
    def inspect(cls, source: str) -> AstInspectionResult:
        """Parse source into an AST and build a navigable hierarchy.

        Args:
            source: Raw Python source code.

        Returns:
            AstInspectionResult with the root node and total count.
        """
        if not source.strip():
            return AstInspectionResult()

        try:
            tree = ast.parse(source)
        except SyntaxError as syntax_error:
            _LOGGER.debug("AST parsing failed: %s", syntax_error)
            return AstInspectionResult(
                syntax_valid=False,
                error=f"SyntaxError at line {syntax_error.lineno}: {syntax_error.msg}",
            )
        except Exception as general_error:
            _LOGGER.error("AST general parse error: %s", general_error, exc_info=True)
            return AstInspectionResult(
                syntax_valid=False,
                error=f"Parse error: {general_error}",
            )

        total_counter = [0]
        root_info = cls._build_node_info(tree, total_counter)
        return AstInspectionResult(
            root=root_info,
            total_nodes=total_counter[0],
            syntax_valid=True,
        )

    @classmethod
    def _build_node_info(
        cls, node: ast.AST, counter: list[int]
    ) -> AstNodeInfo:
        counter[0] += 1
        node_type = type(node).__name__
        node_name = ""
        detail = ""

        if isinstance(node, ast.FunctionDef):
            node_name = f"def {node.name}()"
            doc = ast.get_docstring(node)
            detail = f'docstring: "{doc[:40]}..."' if doc else ""
        elif isinstance(node, ast.AsyncFunctionDef):
            node_name = f"async def {node.name}()"
        elif isinstance(node, ast.ClassDef):
            node_name = f"class {node.name}"
            bases = [ast.unparse(b) for b in node.bases] if hasattr(ast, "unparse") else []
            if bases:
                detail = f"bases: ({', '.join(bases)})"
        elif isinstance(node, ast.Name):
            node_name = node.id
            detail = f"ctx={type(node.ctx).__name__}"
        elif isinstance(node, ast.Constant):
            node_name = repr(node.value)
            if len(node_name) > 60:
                node_name = node_name[:57] + "..."
            detail = f"type={type(node.value).__name__}"
        elif isinstance(node, ast.Attribute):
            node_name = f".{node.attr}"
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
            node_name = f"import {', '.join(names)}"
        elif isinstance(node, ast.ImportFrom):
            names = [alias.name for alias in node.names]
            node_name = f"from {node.module or '.'} import {', '.join(names)}"
        elif isinstance(node, ast.Call):
            func_name = getattr(node.func, "id", getattr(node.func, "attr", "call"))
            node_name = f"{func_name}(...)"
            detail = f"{len(node.args)} args, {len(node.keywords)} kwargs"
        elif isinstance(node, ast.BinOp):
            node_name = type(node.op).__name__
        elif isinstance(node, ast.UnaryOp):
            node_name = type(node.op).__name__
        elif isinstance(node, ast.Compare):
            ops = ", ".join(type(op).__name__ for op in node.ops)
            node_name = ops

        lineno = getattr(node, "lineno", None)
        end_lineno = getattr(node, "end_lineno", None)
        col_offset = getattr(node, "col_offset", None)
        end_col_offset = getattr(node, "end_col_offset", None)

        child_infos: list[AstNodeInfo] = []
        for child in ast.iter_child_nodes(node):
            child_infos.append(cls._build_node_info(child, counter))

        return AstNodeInfo(
            node_type=node_type,
            name=node_name,
            lineno=lineno,
            end_lineno=end_lineno,
            col_offset=col_offset,
            end_col_offset=end_col_offset,
            detail=detail,
            children=child_infos,
        )


# -----------------------------------------------------------------------------
# Symtable Scope Inspector Engine
# -----------------------------------------------------------------------------


class SymtableInspector:
    """Analyzes CPython lexical scoping, variable bindings, and closures."""

    @classmethod
    def inspect(cls, source: str, filename: str = "<editor>") -> SymtableResult:
        """Parse source with `symtable.symtable` and extract nested scopes.

        Args:
            source: Raw Python source code.
            filename: Virtual or physical file name.

        Returns:
            SymtableResult with the root scope and total scope count.
        """
        if not source.strip():
            return SymtableResult()

        try:
            top_table = symtable.symtable(source, filename, "exec")
        except SyntaxError as syntax_error:
            _LOGGER.debug("Symtable parsing failed: %s", syntax_error)
            return SymtableResult(
                error=f"SyntaxError at line {syntax_error.lineno}: {syntax_error.msg}"
            )
        except Exception as general_error:
            _LOGGER.error("Symtable error: %s", general_error, exc_info=True)
            return SymtableResult(error=f"Scope analysis error: {general_error}")

        total_counter = [0]
        root_info = cls._build_scope_info(top_table, total_counter)
        return SymtableResult(root_scope=root_info, total_scopes=total_counter[0])

    @classmethod
    def _build_scope_info(
        cls, table: symtable.SymbolTable, counter: list[int]
    ) -> ScopeInfo:
        counter[0] += 1
        symbols: list[ScopeSymbol] = []
        for symbol_name in table.get_symbols():
            symbols.append(
                ScopeSymbol(
                    name=symbol_name.get_name(),
                    is_local=symbol_name.is_local(),
                    is_global=symbol_name.is_global(),
                    is_nonlocal=symbol_name.is_nonlocal(),
                    is_parameter=symbol_name.is_parameter(),
                    is_free=symbol_name.is_free(),
                    is_cell=symbol_name.is_referenced() and not symbol_name.is_global() and not symbol_name.is_local(),
                    is_assigned=symbol_name.is_assigned(),
                    is_imported=symbol_name.is_imported(),
                )
            )

        children: list[ScopeInfo] = []
        for child_table in table.get_children():
            children.append(cls._build_scope_info(child_table, counter))

        scope_type = table.get_type()  # "module", "function", or "class"
        lineno = table.get_lineno() if hasattr(table, "get_lineno") else 1
        is_nested = table.is_nested() if hasattr(table, "is_nested") else False

        return ScopeInfo(
            name=table.get_name(),
            scope_type=scope_type,
            lineno=lineno,
            is_nested=is_nested,
            symbols=symbols,
            children=children,
        )


# -----------------------------------------------------------------------------
# Execution & Memory Profiler Engine
# -----------------------------------------------------------------------------


class ExecutionProfiler:
    """Executes Python code in a sandboxed harness under cProfile and tracemalloc."""

    @classmethod
    def profile_code(
        cls,
        source: str,
        filename: str = "<profile_target>",
    ) -> ProfileResult:
        """Run source under cProfile and tracemalloc, capturing statistics.

        Args:
            source: Complete executable Python code block.
            filename: Identifier for file location filtering.

        Returns:
            ProfileResult with sortable function records and memory hotspots.
        """
        if not source.strip():
            return ProfileResult()

        try:
            compiled_code = compile(source, filename, "exec")
        except SyntaxError as syntax_error:
            return ProfileResult(
                error=f"SyntaxError at line {syntax_error.lineno}: {syntax_error.msg}"
            )
        except Exception as compile_error:
            return ProfileResult(error=f"Compilation error: {compile_error}")

        # Capture standard outputs during profile execution
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        old_stdout = sys.stdout
        old_stderr = sys.stderr

        profiler = cProfile.Profile()
        tracemalloc.start()

        # Clean, isolated global execution dictionary
        execution_globals = {
            "__name__": "__main__",
            "__file__": filename,
            "__doc__": None,
        }

        execution_error: str | None = None
        try:
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture
            profiler.enable()
            exec(compiled_code, execution_globals)
        except Exception as runtime_error:
            execution_error = f"{type(runtime_error).__name__}: {runtime_error}"
            _LOGGER.info("Profile run encountered runtime exception: %s", runtime_error)
        finally:
            profiler.disable()
            current_mem, peak_mem = tracemalloc.get_traced_memory()
            snapshot = tracemalloc.take_snapshot()
            tracemalloc.stop()
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        # Process function call statistics via pstats
        stats_stream = io.StringIO()
        stats = pstats.Stats(profiler, stream=stats_stream)
        stats.sort_stats("cumulative")

        profile_records: list[ProfileRecord] = []
        for (func_file, func_line, func_name), (cc, nc, tt, ct, _callers) in stats.stats.items():
            # Skip internal cProfile/import machinery to keep results clean
            if "cProfile.py" in func_file:
                continue

            per_call = tt / max(1, nc)
            profile_records.append(
                ProfileRecord(
                    function_name=func_name,
                    filename=Path(func_file).name if func_file else "<unknown>",
                    line=func_line,
                    call_count=nc,
                    total_time_sec=round(tt, 6),
                    cumulative_time_sec=round(ct, 6),
                    per_call_sec=round(per_call, 6),
                )
            )

        # Process tracemalloc memory hotspots
        top_stats = snapshot.statistics("lineno")
        memory_hotspots: list[MemoryHotspot] = []
        for stat in top_stats[:30]:
            frame = stat.traceback[0]
            memory_hotspots.append(
                MemoryHotspot(
                    filename=Path(frame.filename).name,
                    line=frame.lineno,
                    size_bytes=stat.size,
                    count=stat.count,
                )
            )

        # Build hierarchical call tree and relationship maps
        root_call_node, callers_map, callees_map = CallHierarchyBuilder.build_from_pstats(stats)

        return ProfileResult(
            records=profile_records,
            memory_hotspots=memory_hotspots,
            peak_memory_bytes=peak_mem,
            total_duration_sec=round(stats.total_tt, 6),
            stdout=stdout_capture.getvalue(),
            stderr=stderr_capture.getvalue(),
            error=execution_error,
            root_call_node=root_call_node,
            callers_map=callers_map,
            callees_map=callees_map,
        )
