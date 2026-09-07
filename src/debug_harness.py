"""Stand-alone CPython debugger harness for PipViper using bdb.Bdb.

This module is spawned in a separate subprocess under the project's Python
interpreter. It executes the user's script under a custom bdb.Bdb tracing engine
and communicates with the PipViper IDE GUI over stdin/stdout using a structured
JSON-line protocol prefixed with __PIPVIPER_DBG__.
"""

from __future__ import annotations

import argparse
import bdb
import json
import linecache
import os
from pathlib import Path
import sys
import traceback
from typing import Any

PREFIX = "__PIPVIPER_DBG__"

_IGNORED_GLOBALS = frozenset(
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
        "PREFIX",
        "_IGNORED_GLOBALS",
        "inspect_dict",
        "serialize_call_stack",
        "emit_event",
        "PipViperBdb",
        "main",
    }
)


def emit_event(event_name: str, **payload: Any) -> None:
    """Send a structured debugger event to the IDE parent process over stdout."""
    data = {"event": event_name, **payload}
    message = f"{PREFIX}{json.dumps(data)}\n"
    sys.stdout.write(message)
    sys.stdout.flush()


def inspect_dict(mapping: dict[str, Any], is_globals: bool = False) -> list[dict[str, Any]]:
    """Introspect local or global variable mappings into serializable descriptor records."""
    records: list[dict[str, Any]] = []

    for name, value in sorted(mapping.items()):
        if is_globals and (name.startswith("__") or name in _IGNORED_GLOBALS):
            continue
        if name.startswith("__"):
            continue

        type_obj = type(value)
        type_name = getattr(type_obj, "__name__", str(type_obj))

        size_repr = "-"
        if hasattr(value, "shape"):
            try:
                size_repr = str(getattr(value, "shape"))
            except Exception:
                size_repr = "-"
        elif hasattr(value, "__len__"):
            try:
                length = len(value)
                if isinstance(value, (str, bytes)):
                    size_repr = f"{length} chars"
                else:
                    size_repr = f"{length} items"
            except Exception:
                size_repr = "-"

        try:
            memory_bytes = sys.getsizeof(value)
        except Exception:
            memory_bytes = 0

        try:
            val_repr = repr(value)
            if len(val_repr) > 140:
                val_repr = val_repr[:137] + "..."
        except Exception as exc:
            val_repr = f"<repr error: {exc}>"

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


def serialize_call_stack(frame: Any) -> list[dict[str, Any]]:
    """Traverse frame.f_back hierarchy into structured call stack records."""
    frames_list: list[dict[str, Any]] = []
    current = frame
    level = 0

    while current is not None:
        co_filename = current.f_code.co_filename
        lineno = current.f_lineno
        func_name = current.f_code.co_name

        # Attempt to retrieve the source line
        code_line = linecache.getline(co_filename, lineno).strip()

        frames_list.append(
            {
                "level": level,
                "file": co_filename,
                "line": lineno,
                "function": func_name,
                "code_line": code_line,
            }
        )
        current = current.f_back
        level += 1

    return frames_list


class PipViperBdb(bdb.Bdb):
    """Custom Bdb tracer that pauses on breakpoints and steps, awaiting IDE commands."""

    def __init__(self, target_script: str) -> None:
        super().__init__()
        self.target_script: str = os.path.abspath(target_script)
        self.is_paused: bool = False
        self._initial_pause_done: bool = False

    def is_user_code(self, filename: str) -> bool:
        """Determine if a given file belongs to user script rather than internal stdlib/harness."""
        abs_name = os.path.abspath(filename)
        if abs_name == os.path.abspath(__file__):
            return False
        # If it's the target script or inside the target directory, it's user code
        target_dir = os.path.dirname(self.target_script)
        return abs_name == self.target_script or abs_name.startswith(target_dir)

    def user_line(self, frame: Any) -> None:
        """Invoked when execution reaches a candidate line."""
        co_filename = os.path.abspath(frame.f_code.co_filename)

        # Do not pause inside the debugger harness itself or internal bdb machinery
        if co_filename == os.path.abspath(__file__):
            return

        self._pause_and_handle_commands(frame, reason="step")

    def user_exception(self, frame: Any, exc_info: tuple[Any, Any, Any]) -> None:
        """Invoked when an unhandled exception occurs in user code."""
        exc_type, exc_val, _tb = exc_info
        if exc_type is bdb.BdbQuit or exc_type is SystemExit:
            return

        exc_desc = f"{exc_type.__name__}: {exc_val}"
        self._pause_and_handle_commands(frame, reason="exception", exception_detail=exc_desc)

    def _pause_and_handle_commands(
        self,
        frame: Any,
        reason: str = "step",
        exception_detail: str | None = None,
    ) -> None:
        """Emit pause state to the IDE and block synchronously waiting on a command."""
        self.is_paused = True
        co_filename = os.path.abspath(frame.f_code.co_filename)
        lineno = frame.f_lineno

        call_stack = serialize_call_stack(frame)
        locals_data = inspect_dict(frame.f_locals, is_globals=False)
        globals_data = inspect_dict(frame.f_globals, is_globals=True)

        payload: dict[str, Any] = {
            "file": co_filename,
            "line": lineno,
            "reason": reason,
            "call_stack": call_stack,
            "locals": locals_data,
            "globals": globals_data,
        }
        if exception_detail is not None:
            payload["exception"] = exception_detail

        emit_event("paused", **payload)

        # Command loop: read stdin commands from IDE
        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    # Parent process closed stdin -> quit
                    self.set_quit()
                    sys.exit(0)

                line = line.strip()
                if not line:
                    continue

                command_dict = json.loads(line)
                cmd = command_dict.get("cmd")

                if cmd == "continue":
                    emit_event("resumed")
                    self.set_continue()
                    self.is_paused = False
                    break
                elif cmd == "step_into":
                    emit_event("resumed")
                    self.set_step()
                    self.is_paused = False
                    break
                elif cmd == "step_over":
                    emit_event("resumed")
                    self.set_next(frame)
                    self.is_paused = False
                    break
                elif cmd == "step_out":
                    emit_event("resumed")
                    self.set_return(frame)
                    self.is_paused = False
                    break
                elif cmd == "stop":
                    emit_event("terminated", exit_code=-1)
                    self.set_quit()
                    sys.exit(0)
                elif cmd == "set_breakpoints":
                    # Dynamic breakpoint configuration
                    bp_list = command_dict.get("breakpoints", [])
                    self.clear_all_breaks()
                    for bp in bp_list:
                        file_path = bp.get("file")
                        line_no = bp.get("line")
                        if file_path and line_no:
                            self.set_break(file_path, int(line_no))
                    emit_event("breakpoints_updated", count=len(bp_list))
                elif cmd == "get_frame_variables":
                    # Request variables for a specific frame level in the stack
                    target_level = command_dict.get("level", 0)
                    target_frame = frame
                    cur_lvl = 0
                    while target_frame and cur_lvl < target_level:
                        target_frame = target_frame.f_back
                        cur_lvl += 1
                    if target_frame:
                        frame_locals = inspect_dict(target_frame.f_locals, is_globals=False)
                        frame_globals = inspect_dict(target_frame.f_globals, is_globals=True)
                        emit_event(
                            "frame_variables",
                            level=target_level,
                            locals=frame_locals,
                            globals=frame_globals,
                        )
                else:
                    emit_event("unknown_command", command=cmd)

            except Exception as exc:
                emit_event("command_error", error=str(exc))


def main() -> None:
    """Parse CLI arguments and run the target script under PipViperBdb."""
    parser = argparse.ArgumentParser(description="PipViper Debugger Harness")
    parser.add_argument("script", help="Path to Python script to execute")
    parser.add_argument(
        "--breakpoints",
        default="[]",
        help="JSON array of breakpoint objects [{'file': ..., 'line': ...}]",
    )
    parser.add_argument(
        "--stop-on-entry",
        action="store_true",
        help="Whether to pause at the very first line of the user script",
    )
    parser.add_argument("script_args", nargs="*", help="Arguments passed to user script")

    args, unparsed = parser.parse_known_args()

    script_path = os.path.abspath(args.script)
    if not os.path.isfile(script_path):
        emit_event("error", error=f"Target script '{script_path}' not found")
        sys.exit(1)

    debugger = PipViperBdb(script_path)

    # Configure initial breakpoints
    try:
        initial_breakpoints = json.loads(args.breakpoints)
        for bp in initial_breakpoints:
            bp_file = bp.get("file")
            bp_line = bp.get("line")
            if bp_file and bp_line:
                debugger.set_break(bp_file, int(bp_line))
    except Exception as exc:
        emit_event("warning", warning=f"Failed loading initial breakpoints: {exc}")

    if args.stop_on_entry:
        debugger.set_step()

    # Configure sys.argv for the target script
    sys.argv = [script_path] + args.script_args + unparsed
    sys.path.insert(0, os.path.dirname(script_path))

    # Read target script
    try:
        with open(script_path, "rb") as file_handle:
            code_obj = compile(file_handle.read(), script_path, "exec")
    except Exception as exc:
        emit_event("error", error=f"Syntax or compile error in target script: {exc}")
        sys.exit(1)

    target_globals: dict[str, Any] = {
        "__name__": "__main__",
        "__file__": script_path,
        "__builtins__": __builtins__,
        "__doc__": None,
        "__package__": None,
    }

    emit_event("started", file=script_path)

    try:
        debugger.run(code_obj, target_globals)
        emit_event("terminated", exit_code=0)
    except bdb.BdbQuit:
        emit_event("terminated", exit_code=0)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 0
        emit_event("terminated", exit_code=exit_code)
    except Exception as exc:
        tb_str = traceback.format_exc()
        emit_event("error", error=str(exc), traceback=tb_str)
        emit_event("terminated", exit_code=1)


if __name__ == "__main__":
    main()
