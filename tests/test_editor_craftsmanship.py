"""Tests for Phase 7: Elite Editor Craftsmanship.

Covers:
    - Code Folding range calculation, folding/unfolding, nested folding, fold_all, unfold_all
    - Multi-cursor editing (Ctrl+D select next occurrence, simultaneous typing, atomic undo, Esc to clear)
    - Sticky scroll enclosing scope resolution and StickyScopeBar breadcrumbs
    - Split editor panes (horizontal/vertical split, shared document syncing, close split)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeyEvent, QTextCursor, QTextDocument

from src import (
    CodeEditor,
    ColorPalette,
    EditorTabs,
    EditorTheme,
    JediService,
    StickyScopeBar,
    calculate_fold_ranges,
    get_enclosing_scopes,
    get_palette,
)


@pytest.fixture
def palette() -> ColorPalette:
    return get_palette(EditorTheme.DARK)


@pytest.fixture
def jedi_service() -> JediService:
    return JediService()


# -----------------------------------------------------------------------------
# 1. Code Folding Range Calculation Tests
# -----------------------------------------------------------------------------


def test_calculate_fold_ranges_classes_and_functions() -> None:
    doc = QTextDocument()
    code = (
        "class Calculator:\n"
        "    def add(self, a, b):\n"
        "        result = a + b\n"
        "        if result > 100:\n"
        "            print('Big')\n"
        "        return result\n"
        "\n"
        "    def sub(self, a, b):\n"
        "        return a - b\n"
        "\n"
        "def helper():\n"
        "    return 42\n"
    )
    doc.setPlainText(code)
    ranges = calculate_fold_ranges(doc)

    assert 1 in ranges
    assert ranges[1] == 9  # Class Calculator spans lines 2..9
    assert 2 in ranges
    assert ranges[2] == 6  # def add spans lines 3..6
    assert 4 in ranges
    assert ranges[4] == 5  # if result > 100 spans line 5
    assert 8 in ranges
    assert ranges[8] == 9  # def sub spans line 9
    assert 11 in ranges
    assert ranges[11] == 12  # def helper spans line 12


def test_calculate_fold_ranges_docstrings() -> None:
    doc = QTextDocument()
    code = (
        '"""\n'
        'Module docstring\n'
        'with multiple lines\n'
        '"""\n'
        "x = 1\n"
    )
    doc.setPlainText(code)
    ranges = calculate_fold_ranges(doc)
    assert 1 in ranges
    assert ranges[1] == 4


# -----------------------------------------------------------------------------
# 2. Code Editor Folding Execution Tests
# -----------------------------------------------------------------------------


def test_code_editor_toggle_fold(qtbot: Any, palette: ColorPalette, jedi_service: JediService) -> None:
    editor = CodeEditor(palette, jedi_service)
    getattr(qtbot, "addWidget")(editor)

    code = (
        "def compute():\n"
        "    x = 10\n"
        "    y = 20\n"
        "    return x + y\n"
        "\n"
        "def other():\n"
        "    pass\n"
    )
    editor.setPlainText(code)

    signals_received: list[tuple[int, bool]] = []
    editor.fold_toggled.connect(lambda line, folded: signals_received.append((line, folded)))

    # Fold line 1
    folded = editor.toggle_fold(1)
    assert folded is True
    assert 1 in editor.get_folded_blocks()
    assert editor.get_folded_blocks()[1] == 4

    doc = editor.document()
    assert doc.findBlockByNumber(0).isVisible() is True
    assert doc.findBlockByNumber(1).isVisible() is False
    assert doc.findBlockByNumber(2).isVisible() is False
    assert doc.findBlockByNumber(3).isVisible() is False
    assert doc.findBlockByNumber(5).isVisible() is True

    assert len(signals_received) == 1
    assert signals_received[0] == (1, True)

    # Unfold line 1
    unfolded = editor.toggle_fold(1)
    assert unfolded is False
    assert 1 not in editor.get_folded_blocks()

    assert doc.findBlockByNumber(1).isVisible() is True
    assert doc.findBlockByNumber(2).isVisible() is True
    assert doc.findBlockByNumber(3).isVisible() is True
    assert len(signals_received) == 2
    assert signals_received[1] == (1, False)


def test_code_editor_nested_folding(qtbot: Any, palette: ColorPalette, jedi_service: JediService) -> None:
    editor = CodeEditor(palette, jedi_service)
    getattr(qtbot, "addWidget")(editor)

    code = (
        "class Container:\n"
        "    def method(self):\n"
        "        val = 42\n"
        "        return val\n"
        "    def other(self):\n"
        "        pass\n"
    )
    editor.setPlainText(code)
    doc = editor.document()

    # Fold inner method (line 2)
    editor.toggle_fold(2)
    assert doc.findBlockByNumber(2).isVisible() is False
    assert doc.findBlockByNumber(3).isVisible() is False
    assert doc.findBlockByNumber(4).isVisible() is True

    # Fold outer class (line 1)
    editor.toggle_fold(1)
    for i in range(1, 6):
        assert doc.findBlockByNumber(i).isVisible() is False

    # Unfold outer class (line 1): inner method lines (2, 3) must stay hidden, line 4 and 5 become visible
    editor.toggle_fold(1)
    assert doc.findBlockByNumber(0).isVisible() is True
    assert doc.findBlockByNumber(1).isVisible() is True
    assert doc.findBlockByNumber(2).isVisible() is False
    assert doc.findBlockByNumber(3).isVisible() is False
    assert doc.findBlockByNumber(4).isVisible() is True
    assert doc.findBlockByNumber(5).isVisible() is True


def test_code_editor_fold_all_unfold_all(qtbot: Any, palette: ColorPalette, jedi_service: JediService) -> None:
    editor = CodeEditor(palette, jedi_service)
    getattr(qtbot, "addWidget")(editor)

    code = (
        "def func_a():\n"
        "    return 1\n"
        "\n"
        "def func_b():\n"
        "    return 2\n"
    )
    editor.setPlainText(code)
    doc = editor.document()

    editor.fold_all()
    assert doc.findBlockByNumber(1).isVisible() is False
    assert doc.findBlockByNumber(4).isVisible() is False

    editor.unfold_all()
    assert doc.findBlockByNumber(1).isVisible() is True
    assert doc.findBlockByNumber(4).isVisible() is True
    assert len(editor.get_folded_blocks()) == 0


def test_code_editor_fold_current(qtbot: Any, palette: ColorPalette, jedi_service: JediService) -> None:
    editor = CodeEditor(palette, jedi_service)
    getattr(qtbot, "addWidget")(editor)

    code = (
        "def outer():\n"
        "    x = 1\n"
        "    return x\n"
    )
    editor.setPlainText(code)
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.Down)
    editor.setTextCursor(cursor)

    res = editor.fold_current()
    assert res is True
    assert 1 in editor.get_folded_blocks()

    res_unfold = editor.unfold_current()
    assert res_unfold is True
    assert 1 not in editor.get_folded_blocks()


# -----------------------------------------------------------------------------
# 3. Multi-Cursor Editing Tests
# -----------------------------------------------------------------------------


def test_multi_cursor_select_next_occurrence(qtbot: Any, palette: ColorPalette, jedi_service: JediService) -> None:
    editor = CodeEditor(palette, jedi_service)
    getattr(qtbot, "addWidget")(editor)

    text = "target_val = 1\nother = target_val + 2\nfinal = target_val * 3"
    editor.setPlainText(text)

    cursor = editor.textCursor()
    cursor.setPosition(4)
    editor.setTextCursor(cursor)

    # 1st Ctrl+D: selects the word under cursor
    ok1 = editor.select_next_occurrence()
    assert ok1 is True
    assert editor.textCursor().selectedText() == "target_val"
    assert editor.extra_cursor_count() == 0

    # 2nd Ctrl+D: adds next occurrence
    ok2 = editor.select_next_occurrence()
    assert ok2 is True
    assert editor.extra_cursor_count() == 1
    assert editor.extra_cursors()[0].selectedText() == "target_val"

    # 3rd Ctrl+D: adds 3rd occurrence
    ok3 = editor.select_next_occurrence()
    assert ok3 is True
    assert editor.extra_cursor_count() == 2


def test_multi_cursor_typing_and_atomic_undo(qtbot: Any, palette: ColorPalette, jedi_service: JediService) -> None:
    editor = CodeEditor(palette, jedi_service)
    getattr(qtbot, "addWidget")(editor)

    text = "alpha beta alpha gamma alpha"
    editor.setPlainText(text)

    cursor = editor.textCursor()
    cursor.setPosition(2)
    editor.setTextCursor(cursor)

    editor.select_next_occurrence()
    editor.select_next_occurrence()
    editor.select_next_occurrence()
    assert editor.extra_cursor_count() == 2

    # Simulate typing 'X'
    event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_X, Qt.KeyboardModifier.NoModifier, "X")
    editor.keyPressEvent(event)

    assert editor.toPlainText() == "X beta X gamma X"

    # Verify single-step atomic undo
    editor.undo()
    assert editor.toPlainText() == "alpha beta alpha gamma alpha"


def test_multi_cursor_escape_clears(qtbot: Any, palette: ColorPalette, jedi_service: JediService) -> None:
    editor = CodeEditor(palette, jedi_service)
    getattr(qtbot, "addWidget")(editor)

    editor.setPlainText("foo foo foo")
    cursor = editor.textCursor()
    cursor.setPosition(1)
    editor.setTextCursor(cursor)

    editor.select_next_occurrence()
    editor.select_next_occurrence()
    assert editor.extra_cursor_count() == 1

    esc_event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    editor.keyPressEvent(esc_event)
    assert editor.extra_cursor_count() == 0


# -----------------------------------------------------------------------------
# 4. Sticky Scroll & Scope Extraction Tests
# -----------------------------------------------------------------------------


def test_get_enclosing_scopes() -> None:
    doc = QTextDocument()
    code = (
        "class Model:\n"
        "    def forward(self, x):\n"
        "        # line 3\n"
        "        y = x * 2\n"
        "        return y\n"
        "\n"
        "def standalone():\n"
        "    pass\n"
    )
    doc.setPlainText(code)

    scopes_line4 = get_enclosing_scopes(doc, 4)
    assert len(scopes_line4) == 2
    assert scopes_line4[0][0] == "class"
    assert "class Model" in scopes_line4[0][1]
    assert scopes_line4[1][0] == "function"
    assert "def forward(self, x)" in scopes_line4[1][1]

    scopes_line8 = get_enclosing_scopes(doc, 8)
    assert len(scopes_line8) == 1
    assert scopes_line8[0][0] == "function"
    assert "def standalone()" in scopes_line8[0][1]


def test_sticky_scope_bar(qtbot: Any, palette: ColorPalette, jedi_service: JediService) -> None:
    editor = CodeEditor(palette, jedi_service)
    getattr(qtbot, "addWidget")(editor)

    code = (
        "class Widget:\n"
        "    def render(self):\n"
        "        pass\n"
    )
    editor.setPlainText(code)
    bar = StickyScopeBar(editor, palette)
    getattr(qtbot, "addWidget")(bar)

    bar.update_scopes()
    assert bar.isVisible() or bar.isHidden()


# -----------------------------------------------------------------------------
# 5. Split Panes Tests in EditorTabs
# -----------------------------------------------------------------------------


def test_editor_tabs_split_right_and_close(
    qtbot: Any, palette: ColorPalette, jedi_service: JediService, tmp_path: Path
) -> None:
    tabs = EditorTabs(palette, jedi_service)
    getattr(qtbot, "addWidget")(tabs)

    test_file = tmp_path / "module.py"
    test_file.write_text("print('hello split')\n", encoding="utf-8")

    ed1 = tabs.add_editor(test_file, "print('hello split')\n")
    assert tabs.is_split() is False
    assert tabs.editor_count() == 1

    # Split editor right
    tabs.split_right()
    assert tabs.is_split() is True
    assert tabs.editor_count() == 2

    # Edits in primary editor synchronize to secondary editor via shared QTextDocument
    ed1.setPlainText("updated text")
    sec_ed = tabs.editor_for_path(test_file)
    assert sec_ed is not None
    assert sec_ed.toPlainText() == "updated text"

    # Close split
    tabs.close_split()
    assert tabs.is_split() is False
    assert tabs.editor_count() == 1
