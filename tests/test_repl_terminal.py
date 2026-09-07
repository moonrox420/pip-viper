"""Unit tests for Phase 2: REPL harness, Live Variable Explorer, TerminalPanel, and Editor REPL shortcuts.

Validates the interactive console harness, JSON variable serialization,
filtering, double-click inspection, terminal process lifecycle, and editor keybindings.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QTextCursor
from PySide6.QtWidgets import QTableWidgetItem

from src import EditorTheme, JediService, get_palette
from src.editor import CodeEditor, EditorTabs
from src.panels import ReplPanel, ReplVariable, TerminalPanel
from src.repl_harness import PipViperConsole, inspect_namespace


# -----------------------------------------------------------------------------
# 1. REPL Harness & Namespace Introspection Tests
# -----------------------------------------------------------------------------


def test_inspect_namespace_standard_types() -> None:
    """Verify standard types are correctly serialized with types, sizes, and previews."""
    test_ns = {
        "an_int": 42,
        "a_float": 3.14159,
        "a_string": "PipViper Station",
        "a_list": [1, 2, 3, 4, 5],
        "a_dict": {"alpha": 1, "beta": 2},
        "_private_var": 999,
        "__doc__": "module documentation",
    }

    variables = inspect_namespace(test_ns)
    # Also verify json round-trip matches wire protocol
    wire_json = json.dumps(variables)
    assert isinstance(wire_json, str)
    var_dict = {v["name"]: v for v in variables}

    # Private and dunder entries must be omitted
    assert "_private_var" not in var_dict
    assert "__doc__" not in var_dict

    # Verify standard types
    assert var_dict["an_int"]["type_name"] == "int"
    assert var_dict["an_int"]["value_preview"] == "42"

    assert var_dict["a_float"]["type_name"] == "float"

    assert var_dict["a_string"]["type_name"] == "str"
    assert var_dict["a_string"]["size_repr"] == "16 chars"

    assert var_dict["a_list"]["type_name"] == "list"
    assert var_dict["a_list"]["size_repr"] == "5 items"

    assert var_dict["a_dict"]["type_name"] == "dict"
    assert var_dict["a_dict"]["size_repr"] == "2 items"

    # All parsed objects must validate under ReplVariable
    for var in variables:
        model = ReplVariable(**var)
        assert model.name in var_dict


def test_inspect_namespace_defensive_against_broken_repr() -> None:
    """Verify that an object whose __repr__ raises does not break serialization."""

    class EvilRepr:
        def __repr__(self) -> str:
            raise RuntimeError("Boom in repr!")

    test_ns = {"bad_obj": EvilRepr(), "good_num": 100}
    variables = inspect_namespace(test_ns)
    var_dict = {v["name"]: v for v in variables}

    assert "good_num" in var_dict
    assert "bad_obj" in var_dict
    assert "<repr error" in var_dict["bad_obj"]["value_preview"]


def test_repl_harness_block_execution() -> None:
    """Verify PipViperConsole compiles and runs multi-line code blocks."""
    console = PipViperConsole()
    block = "x = 15\ny = 25\nz = x * y\n"
    console.execute_block(block)

    assert console.locals.get("x") == 15
    assert console.locals.get("y") == 25
    assert console.locals.get("z") == 375


# -----------------------------------------------------------------------------
# 2. ReplPanel & Live Variable Explorer Tests
# -----------------------------------------------------------------------------


def test_repl_panel_initialization_and_palette(qtbot: object) -> None:
    """Verify ReplPanel widgets instantiate and receive theme updates."""
    palette = get_palette(EditorTheme.DARK)
    panel = ReplPanel(palette, sys.executable)
    getattr(qtbot, "addWidget")(panel)

    assert panel._input_field is not None
    assert panel._output_view is not None
    assert panel._variables_table is not None
    assert panel._var_filter_input is not None

    # Test set_palette
    light_palette = get_palette(EditorTheme.LIGHT)
    panel.set_palette(light_palette)
    assert light_palette.background in panel._output_view.styleSheet()

    panel.cleanup()


def test_repl_panel_parse_variables_and_filtering(qtbot: object) -> None:
    """Verify variables JSON parsing and substring search filtering."""
    palette = get_palette(EditorTheme.DARK)
    panel = ReplPanel(palette, sys.executable)
    getattr(qtbot, "addWidget")(panel)

    sample_json = json.dumps(
        [
            {
                "name": "matrix_a",
                "type_name": "list",
                "size_repr": "len 10",
                "memory_bytes": 128,
                "value_preview": "[[...]]",
            },
            {
                "name": "matrix_b",
                "type_name": "list",
                "size_repr": "len 10",
                "memory_bytes": 128,
                "value_preview": "[[...]]",
            },
            {
                "name": "total_sum",
                "type_name": "int",
                "size_repr": "-",
                "memory_bytes": 28,
                "value_preview": "999",
            },
        ]
    )

    panel._parse_variables_json(sample_json)
    assert len(panel._all_variables) == 3
    assert panel._variables_table.rowCount() == 3

    # Filter for 'matrix'
    panel._apply_variable_filter("matrix")
    assert panel._variables_table.rowCount() == 2

    # Filter for 'sum'
    panel._apply_variable_filter("sum")
    assert panel._variables_table.rowCount() == 1
    item = panel._variables_table.item(0, 0)
    assert item is not None
    assert item.text() == "total_sum"

    # Reset filter
    panel._apply_variable_filter("")
    assert panel._variables_table.rowCount() == 3

    panel.cleanup()


def test_repl_panel_variable_double_click(qtbot: object) -> None:
    """Verify double-clicking a variable item triggers variable_activated and prints."""
    palette = get_palette(EditorTheme.DARK)
    panel = ReplPanel(palette, sys.executable)
    getattr(qtbot, "addWidget")(panel)

    sample_json = json.dumps(
        [
            {
                "name": "target_var",
                "type_name": "str",
                "size_repr": "5 chars",
                "memory_bytes": 54,
                "value_preview": "world",
            }
        ]
    )
    panel._parse_variables_json(sample_json)

    received_vars: list[str] = []
    panel.variable_activated.connect(received_vars.append)

    item = panel._variables_table.item(0, 0)
    assert item is not None
    panel._on_variable_double_clicked(item)

    assert received_vars == ["target_var"]
    panel.cleanup()


def test_repl_panel_execute_code_formatting(qtbot: object) -> None:
    """Verify execute_code formats single and multi-line commands in output."""
    palette = get_palette(EditorTheme.DARK)
    panel = ReplPanel(palette, sys.executable)
    getattr(qtbot, "addWidget")(panel)

    # Single line
    panel.execute_code("x = 42")
    assert ">>> x = 42" in panel._output_view.toPlainText()

    # Multi line
    panel.execute_code("def greet():\n    return 'hi'")
    text = panel._output_view.toPlainText()
    assert ">>> def greet():" in text
    assert "...     return 'hi'" in text

    panel.cleanup()


# -----------------------------------------------------------------------------
# 3. TerminalPanel Tests
# -----------------------------------------------------------------------------


def test_terminal_panel_lifecycle_and_root(qtbot: object, tmp_path: Path) -> None:
    """Verify TerminalPanel setup, palette updating, and project root directory change."""
    palette = get_palette(EditorTheme.DARK)
    panel = TerminalPanel(palette)
    getattr(qtbot, "addWidget")(panel)

    assert panel._terminal_view is not None
    assert panel._input_field is not None

    # Test palette update
    light_palette = get_palette(EditorTheme.LIGHT)
    panel.set_palette(light_palette)
    assert light_palette.background in panel._terminal_view.styleSheet()

    # Test project root change
    test_project = tmp_path / "sample_project"
    test_project.mkdir()
    panel.set_project_root(test_project)
    assert "sample_project" in panel._cwd_label.text()

    panel.cleanup()


def test_terminal_panel_history_navigation(qtbot: object) -> None:
    """Verify Up and Down arrow keys navigate the command history in the input field."""
    palette = get_palette(EditorTheme.DARK)
    panel = TerminalPanel(palette)
    getattr(qtbot, "addWidget")(panel)

    panel._history = ["git status", "pytest -v", "uv pip list"]
    panel._history_index = -1

    # Simulate Up arrow: should show last command "uv pip list"
    up_event = QKeyEvent(
        QEvent.Type.KeyPress,
        int(Qt.Key.Key_Up),
        Qt.KeyboardModifier.NoModifier,
    )
    handled = panel.eventFilter(panel._input_field, up_event)
    assert handled is True
    assert panel._input_field.text() == "uv pip list"

    # Simulate Up arrow again: should show "pytest -v"
    panel.eventFilter(panel._input_field, up_event)
    assert panel._input_field.text() == "pytest -v"

    # Simulate Down arrow: should go forward to "uv pip list"
    down_event = QKeyEvent(
        QEvent.Type.KeyPress,
        int(Qt.Key.Key_Down),
        Qt.KeyboardModifier.NoModifier,
    )
    panel.eventFilter(panel._input_field, down_event)
    assert panel._input_field.text() == "uv pip list"

    # Down arrow past end clears input
    panel.eventFilter(panel._input_field, down_event)
    assert panel._input_field.text() == ""

    panel.cleanup()


# -----------------------------------------------------------------------------
# 4. CodeEditor & EditorTabs "Send to REPL" Shortcut Tests
# -----------------------------------------------------------------------------


def test_code_editor_send_selection_to_repl(qtbot: object) -> None:
    """Verify Ctrl+Enter emits selected text to REPL."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    editor = CodeEditor(palette, jedi)
    getattr(qtbot, "addWidget")(editor)

    editor.setPlainText("alpha = 10\nbeta = 20\ngamma = alpha + beta\n")

    emitted_code: list[str] = []
    editor.send_to_repl_requested.connect(emitted_code.append)

    # Select line 1: "alpha = 10"
    cursor = editor.textCursor()
    cursor.setPosition(0)
    cursor.movePosition(
        QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor
    )
    editor.setTextCursor(cursor)

    # Press Ctrl+Enter
    getattr(qtbot, "keyPress")(
        editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier
    )

    assert len(emitted_code) == 1
    assert emitted_code[0] == "alpha = 10"


def test_code_editor_send_current_line_to_repl_when_no_selection(qtbot: object) -> None:
    """Verify Shift+Enter without selection emits the active line under cursor."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    editor = CodeEditor(palette, jedi)
    getattr(qtbot, "addWidget")(editor)

    editor.setPlainText("first_line = 1\nsecond_line = 2\n")

    emitted_code: list[str] = []
    editor.send_to_repl_requested.connect(emitted_code.append)

    # Place cursor on line 2 without selecting anything
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.NextBlock)
    editor.setTextCursor(cursor)
    assert not cursor.hasSelection()

    # Press Shift+Enter
    getattr(qtbot, "keyPress")(
        editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier
    )

    assert len(emitted_code) == 1
    assert emitted_code[0] == "second_line = 2"


def test_editor_tabs_forwards_send_to_repl_signal(qtbot: object, tmp_path: Path) -> None:
    """Verify EditorTabs forwards send_to_repl_requested from child editors."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    tabs = EditorTabs(palette, jedi)
    getattr(qtbot, "addWidget")(tabs)

    test_file = tmp_path / "script.py"
    editor = tabs.add_editor(test_file, "result = 123\n")

    emitted_code: list[str] = []
    tabs.send_to_repl_requested.connect(emitted_code.append)

    editor.send_to_repl_requested.emit("result = 123")
    assert emitted_code == ["result = 123"]


def test_main_window_repl_and_terminal_integration(qtbot: object, tmp_path: Path) -> None:
    """Verify MainWindow integrates ReplPanel and TerminalPanel, handles Send to REPL."""
    from src.app import MainWindow
    from src import AppConfig

    config = AppConfig(log_directory=tmp_path / "logs")
    window = MainWindow(config)
    getattr(qtbot, "addWidget")(window)

    # Test bottom tabs contain REPL and Terminal
    assert window._repl_panel is not None
    assert window._terminal_panel is not None

    # Test show_repl_panel
    window._show_repl_panel()
    assert window._bottom_tabs.currentWidget() is window._repl_panel

    # Test show_terminal_panel
    window._show_terminal_panel()
    assert window._bottom_tabs.currentWidget() is window._terminal_panel

    # Test on_send_to_repl switches to REPL and executes
    window._on_send_to_repl("val = 99")
    assert window._bottom_tabs.currentWidget() is window._repl_panel
    assert ">>> val = 99" in window._repl_panel._output_view.toPlainText()

    # Clean up processes
    window._repl_panel.cleanup()
    window._terminal_panel.cleanup()
    window.close()

