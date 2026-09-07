"""Unit tests for Phase 3: Visual Graphical Debugger.

Validates the debug harness, gutter breakpoint margin, execution line pointers,
call stack navigation, variables inspection tree, breakpoint manager, and IDE stepping controls.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QListWidgetItem

from src import AppConfig, EditorTheme, JediService, get_palette
from src.app import MainWindow
from src.debug_harness import (
    inspect_dict,
    serialize_call_stack,
)
from src.editor import CodeEditor, EditorTabs
from src.panels import DebugPanel


# -----------------------------------------------------------------------------
# 1. Debug Harness & Introspection Tests
# -----------------------------------------------------------------------------


def test_debug_harness_inspect_dict_standard_and_defensive() -> None:
    """Verify inspect_dict extracts types, shapes, and handles bad __repr__ gracefully."""

    class EvilClass:
        def __repr__(self) -> str:
            raise ValueError("Failing repr!")

    test_scope = {
        "x": 42,
        "pi": 3.14159,
        "items": [1, 2, 3],
        "name": "PipViper Station",
        "evil": EvilClass(),
        "__dunder__": "should be skipped",
    }

    records = inspect_dict(test_scope, is_globals=True)
    rec_dict = {r["name"]: r for r in records}

    assert "__dunder__" not in rec_dict
    assert rec_dict["x"]["type_name"] == "int"
    assert rec_dict["x"]["value_preview"] == "42"

    assert rec_dict["items"]["type_name"] == "list"
    assert rec_dict["items"]["size_repr"] == "3 items"

    assert rec_dict["name"]["size_repr"] == "16 chars"
    assert "<repr error" in rec_dict["evil"]["value_preview"]


def test_debug_harness_serialize_call_stack() -> None:
    """Verify serialize_call_stack traverses current stack frames correctly."""

    def level_two() -> list[dict[str, object]]:
        frame = sys._getframe()
        return serialize_call_stack(frame)

    def level_one() -> list[dict[str, object]]:
        return level_two()

    stack = level_one()
    assert len(stack) >= 2
    assert stack[0]["function"] == "level_two"
    assert stack[0]["level"] == 0
    assert stack[1]["function"] == "level_one"
    assert stack[1]["level"] == 1


# -----------------------------------------------------------------------------
# 2. CodeEditor & LineNumberArea Gutter Breakpoint Tests
# -----------------------------------------------------------------------------


def test_code_editor_breakpoint_toggle_and_signals(qtbot: object) -> None:
    """Verify toggle_breakpoint adds/removes lines and emits breakpoint_toggled."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    editor = CodeEditor(palette, jedi)
    getattr(qtbot, "addWidget")(editor)

    editor.setPlainText("line 1\nline 2\nline 3\nline 4\n")

    events: list[tuple[object, int, bool]] = []
    editor.breakpoint_toggled.connect(lambda fp, ln, is_set: events.append((fp, ln, is_set)))

    # Toggle line 2 ON
    is_set = editor.toggle_breakpoint(2)
    assert is_set is True
    assert 2 in editor.get_breakpoints()
    assert len(events) == 1
    assert events[0][1] == 2
    assert events[0][2] is True

    # Toggle line 2 OFF
    is_set = editor.toggle_breakpoint(2)
    assert is_set is False
    assert 2 not in editor.get_breakpoints()
    assert len(events) == 2
    assert events[1][2] is False


def test_code_editor_gutter_mouse_click(qtbot: object) -> None:
    """Verify mouse clicks on the LineNumberArea gutter toggle breakpoints."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    editor = CodeEditor(palette, jedi)
    getattr(qtbot, "addWidget")(editor)

    editor.setPlainText("first = 1\nsecond = 2\nthird = 3\n")
    gutter = editor._line_number_area

    # Calculate approximate Y position of line 2
    block = editor.document().findBlockByNumber(1)
    rect = editor.blockBoundingGeometry(block)
    click_y = int(rect.top() + rect.height() / 2)

    from PySide6.QtCore import QPointF

    click_event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(5.0, float(click_y)),
        QPointF(5.0, float(click_y)),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    gutter.mousePressEvent(click_event)

    # Line 2 should now have a breakpoint
    assert 2 in editor.get_breakpoints()

    # Click again to clear
    gutter.mousePressEvent(click_event)
    assert 2 not in editor.get_breakpoints()


def test_code_editor_execution_pointer_and_highlight(qtbot: object) -> None:
    """Verify set_execution_line sets execution line and updates extra selections."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    editor = CodeEditor(palette, jedi)
    getattr(qtbot, "addWidget")(editor)

    editor.setPlainText("def foo():\n    return 42\n")

    editor.set_execution_line(2)
    assert editor._execution_line == 2
    # At least current line and execution line selections must exist
    assert len(editor.extraSelections()) >= 1

    editor.set_execution_line(None)
    assert editor._execution_line is None


# -----------------------------------------------------------------------------
# 3. EditorTabs Breakpoint Management Tests
# -----------------------------------------------------------------------------


def test_editor_tabs_breakpoint_aggregation(qtbot: object, tmp_path: Path) -> None:
    """Verify EditorTabs aggregates breakpoints across multiple files."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    tabs = EditorTabs(palette, jedi)
    getattr(qtbot, "addWidget")(tabs)

    f1 = tmp_path / "module_a.py"
    f2 = tmp_path / "module_b.py"

    ed1 = tabs.add_editor(f1, "a = 1\nb = 2\n")
    ed2 = tabs.add_editor(f2, "x = 10\ny = 20\n")

    ed1.toggle_breakpoint(1)
    ed1.toggle_breakpoint(2)
    ed2.toggle_breakpoint(2)

    all_bps = tabs.get_all_breakpoints()
    assert ed1.file_path().resolve() in all_bps
    assert ed2.file_path().resolve() in all_bps
    assert all_bps[ed1.file_path().resolve()] == {1, 2}
    assert all_bps[ed2.file_path().resolve()] == {2}

    # Test set_execution_line on module_b switches tab and sets pointer
    tabs.set_execution_line(f2, 2)
    assert tabs.current_editor() is ed2
    assert ed2._execution_line == 2
    assert ed1._execution_line is None

    # Clear all
    tabs.clear_all_execution_lines()
    assert ed2._execution_line is None


# -----------------------------------------------------------------------------
# 4. DebugPanel UI & Stepping Controls Tests
# -----------------------------------------------------------------------------


def test_debug_panel_session_states(qtbot: object) -> None:
    """Verify DebugPanel updates button states across inactive, running, and paused."""
    palette = get_palette(EditorTheme.DARK)
    panel = DebugPanel(palette)
    getattr(qtbot, "addWidget")(panel)

    # Inactive initially
    assert panel._start_btn.isEnabled()
    assert not panel._step_over_btn.isEnabled()
    assert not panel._stop_btn.isEnabled()

    # Running
    panel.set_session_state("running", file="app.py")
    assert not panel._start_btn.isEnabled()
    assert not panel._step_over_btn.isEnabled()
    assert panel._stop_btn.isEnabled()
    assert "RUNNING" in panel._status_label.text()

    # Paused
    panel.set_session_state("paused", file="app.py", line=14)
    assert panel._start_btn.isEnabled()
    assert panel._start_btn.text() == "▷ Continue (F5)"
    assert panel._step_over_btn.isEnabled()
    assert panel._step_into_btn.isEnabled()
    assert panel._step_out_btn.isEnabled()
    assert panel._stop_btn.isEnabled()
    assert "PAUSED at line 14" in panel._status_label.text()

    # Inactive
    panel.set_session_state("inactive")
    assert panel._start_btn.isEnabled()
    assert panel._start_btn.text() == "▷ Start (F5)"
    assert not panel._step_over_btn.isEnabled()


def test_debug_panel_call_stack_and_variables(qtbot: object) -> None:
    """Verify call stack and variables tree display and filtering."""
    palette = get_palette(EditorTheme.DARK)
    panel = DebugPanel(palette)
    getattr(qtbot, "addWidget")(panel)

    frames = [
        {
            "level": 0,
            "file": "calculator.py",
            "line": 25,
            "function": "multiply",
            "code_line": "return a * b",
        },
        {
            "level": 1,
            "file": "main.py",
            "line": 10,
            "function": "main",
            "code_line": "multiply(5, 6)",
        },
    ]
    panel.update_call_stack(frames)
    assert panel._call_stack_list.count() == 2

    # Click stack item emits signals
    jump_coords: list[tuple[str, int]] = []
    panel.frame_jump_requested.connect(lambda f, l: jump_coords.append((f, l)))

    item = panel._call_stack_list.item(0)
    assert item is not None
    panel._on_stack_item_clicked(item)
    assert len(jump_coords) == 1
    assert jump_coords[0] == ("calculator.py", 25)

    # Variables inspection
    locals_data = [
        {"name": "factor", "type_name": "int", "size_repr": "-", "memory_bytes": 28, "value_preview": "7"},
        {"name": "greeting", "type_name": "str", "size_repr": "5 chars", "memory_bytes": 54, "value_preview": "'hello'"},
    ]
    globals_data = [
        {"name": "MAX_ITEMS", "type_name": "int", "size_repr": "-", "memory_bytes": 28, "value_preview": "100"}
    ]
    panel.update_variables(locals_data, globals_data)
    assert panel._variables_tree.topLevelItemCount() == 2

    # Filter variables for "greet"
    panel._apply_var_filter("greet")
    locals_root = panel._variables_tree.topLevelItem(0)
    assert locals_root is not None
    assert locals_root.childCount() == 1
    assert locals_root.child(0).text(0) == "greeting"


def test_debug_panel_breakpoints_table(qtbot: object) -> None:
    """Verify breakpoints table renders items and delete button emits signal."""
    palette = get_palette(EditorTheme.DARK)
    panel = DebugPanel(palette)
    getattr(qtbot, "addWidget")(panel)

    bps = {Path("sample.py"): {10, 20}}
    panel.update_breakpoints(bps)
    assert panel._breakpoints_table.rowCount() == 2

    removed_bps: list[tuple[str, int]] = []
    panel.breakpoint_removed.connect(lambda f, l: removed_bps.append((f, l)))

    # Trigger delete button on row 0
    btn = panel._breakpoints_table.cellWidget(0, 2)
    assert btn is not None
    getattr(qtbot, "mouseClick")(btn, Qt.MouseButton.LeftButton)

    assert len(removed_bps) == 1
    assert removed_bps[0][1] in (10, 20)


# -----------------------------------------------------------------------------
# 5. MainWindow Debugger Orchestration Tests
# -----------------------------------------------------------------------------


def test_main_window_debugger_orchestration(qtbot: object, tmp_path: Path) -> None:
    """Verify MainWindow integrates DebugPanel, breakpoint toggle, and stepping shortcuts."""
    config = AppConfig(log_directory=tmp_path / "logs")
    window = MainWindow(config)
    getattr(qtbot, "addWidget")(window)

    editor = window._editor_tabs.add_editor(tmp_path / "demo.py", "x = 10\ny = 20\n")

    # Toggle breakpoint via menu action shortcut
    window._toggle_current_line_breakpoint()
    assert 1 in editor.get_breakpoints()
    assert window._debug_panel._breakpoints_table.rowCount() == 1

    # Simulate debugger event: paused
    fake_payload = {
        "event": "paused",
        "file": str(tmp_path / "demo.py"),
        "line": 1,
        "call_stack": [{"level": 0, "file": str(tmp_path / "demo.py"), "line": 1, "function": "<module>"}],
        "locals": [{"name": "x", "type_name": "int", "size_repr": "-", "memory_bytes": 28, "value_preview": "10"}],
        "globals": [],
    }
    window._handle_debugger_event(fake_payload)
    assert window._debug_panel._is_paused is True
    assert editor._execution_line == 1

    # Simulate debugger event: resumed
    window._handle_debugger_event({"event": "resumed"})
    assert window._debug_panel._is_paused is False
    assert editor._execution_line is None

    # Cleanup
    window._stop_debugger()
    window._repl_panel.cleanup()
    window._terminal_panel.cleanup()
    window.close()

