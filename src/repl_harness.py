"""Interactive REPL harness for PipViper with namespace introspection protocol.

This harness runs in a subprocess under the project's Python interpreter.
It executes user code using an InteractiveConsole, and after each statement
or batch of statements, it emits a structured JSON protocol block containing
all active global/local variables for the Live Variable Explorer.
"""

from __future__ import annotations

import code
import json
import sys
import traceback
from typing import Any

START_MARKER = "__PIPVIPER_VARS_START__"
END_MARKER = "__PIPVIPER_VARS_END__"

# Variables to ignore in the variable explorer
_IGNORED_NAMES = frozenset(
    {
        "__name__",
        "__doc__",
        "__package__",
        "__loader__",
        "__spec__",
        "__annotations__",
        "__builtins__",
        "__file__",
        "__cached__",
        "START_MARKER",
        "END_MARKER",
        "_IGNORED_NAMES",
        "inspect_namespace",
        "emit_variables",
        "PipViperConsole",
        "main",
    }
)


def inspect_namespace(namespace: dict[str, Any]) -> list[dict[str, Any]]:
    """Introspect all user variables in the given namespace into structured records."""
    records: list[dict[str, Any]] = []

    for name, value in sorted(namespace.items()):
        if (name.startswith("_") and name != "_") or name in _IGNORED_NAMES:
            continue

        # Determine type name
        type_obj = type(value)
        type_name = getattr(type_obj, "__name__", str(type_obj))

        # Determine shape or length
        size_repr = "-"
        if hasattr(value, "shape"):
            try:
                size_repr = str(getattr(value, "shape"))
            except Exception:
                pass
        elif hasattr(value, "__len__"):
            try:
                length = len(value)
                if isinstance(value, (str, bytes)):
                    size_repr = f"{length} chars"
                else:
                    size_repr = f"{length} items"
            except Exception:
                pass

        # Determine memory size
        memory_bytes = 0
        try:
            memory_bytes = sys.getsizeof(value)
        except Exception:
            memory_bytes = 0

        # Create safe value preview
        try:
            val_repr = repr(value)
            if len(val_repr) > 120:
                val_repr = val_repr[:117] + "..."
        except Exception as repr_err:
            val_repr = f"<repr error: {repr_err}>"

        records.append(
            {
                "name": name,
                "type_name": type_name,
                "size_repr": size_repr,
                "memory_bytes": memory_bytes,
                "value_preview": val_repr,
            }
        )

    return records


def emit_variables(namespace: dict[str, Any]) -> None:
    """Serialize and flush active namespace variables between protocol delimiters."""
    try:
        variables = inspect_namespace(namespace)
        payload = json.dumps(variables)
        sys.stdout.write(f"\n{START_MARKER}\n{payload}\n{END_MARKER}\n")
        sys.stdout.flush()
    except Exception as exc:
        sys.stderr.write(f"Failed to serialize REPL variables: {exc}\n")
        sys.stderr.flush()


class PipViperConsole(code.InteractiveConsole):
    """Custom InteractiveConsole that triggers namespace emission after execution."""

    def __init__(self, locals_dict: dict[str, Any] | None = None) -> None:
        super().__init__(locals=locals_dict)

    def runcode(self, code_obj: Any) -> None:
        """Execute a code object and emit updated variables on success or error."""
        try:
            exec(code_obj, self.locals)
        except SystemExit:
            raise
        except Exception:
            self.showtraceback()
        emit_variables(self.locals)

    def run_multi_line_block(self, source: str) -> None:
        """Compile and execute an arbitrary multi-line block cleanly."""
        try:
            # First try compiling as single statement / eval
            compiled = compile(source, "<repl>", "single")
        except SyntaxError:
            try:
                # If single-mode fails, compile in exec mode (for multi-statements, defs, loops)
                compiled = compile(source, "<repl>", "exec")
            except Exception:
                self.showsyntaxerror("<repl>")
                emit_variables(self.locals)
                return

        self.runcode(compiled)

    execute_block = run_multi_line_block


def main() -> None:
    """Run the interactive REPL loop reading from stdin."""
    console = PipViperConsole()

    # Emit initial namespace
    emit_variables(console.locals)

    buffer: list[str] = []

    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break  # EOF

            stripped = line.rstrip("\r\n")

            if stripped == "__pipviper_inspect__()":
                emit_variables(console.locals)
                continue

            # Special multi-line batch protocol:
            # __PIPVIPER_BLOCK_START__
            # ...
            # __PIPVIPER_BLOCK_END__
            if stripped == "__PIPVIPER_BLOCK_START__":
                block_lines: list[str] = []
                while True:
                    next_line = sys.stdin.readline()
                    if not next_line:
                        break
                    if next_line.rstrip("\r\n") == "__PIPVIPER_BLOCK_END__":
                        break
                    block_lines.append(next_line)
                console.run_multi_line_block("".join(block_lines))
                continue

            # Standard line-by-line interactive handling
            buffer.append(stripped)
            source = "\n".join(buffer)

            # Try to push to console
            more = console.push(stripped)
            if not more:
                buffer.clear()

        except KeyboardInterrupt:
            buffer.clear()
            sys.stderr.write("\nKeyboardInterrupt\n")
            sys.stderr.flush()
            emit_variables(console.locals)
        except Exception as exc:
            buffer.clear()
            sys.stderr.write(f"\nUnhandled harness exception: {exc}\n")
            sys.stderr.flush()
            emit_variables(console.locals)


if __name__ == "__main__":
    main()
