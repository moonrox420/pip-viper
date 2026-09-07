"""Tests for the CodeEditor component and auto-indentation engine.

Uses pytest-qt to simulate standard interactive user keyboard strokes
and verifies that editor responses remain immediate and accurate.
"""

from __future__ import annotations


from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor

from src import get_palette, EditorTheme, JediService
from src.editor import CodeEditor


def test_code_editor_auto_indent(qtbot: object) -> None:
    """Verify that pressing return on colon ends correctly prepends indentation."""
    palette = get_palette(EditorTheme.DARK)
    jedi_service = JediService()
    editor = CodeEditor(palette, jedi_service)

    # Register the widget for execution lifecycle tracking
    getattr(qtbot, "addWidget")(editor)  # type: ignore[attr-defined]

    # Type definition signature ending with colon
    editor.setPlainText("def main_wrapper():")
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    editor.setTextCursor(cursor)

    # Simulate keyboard return action
    getattr(qtbot, "keyPress")(editor, Qt.Key.Key_Return)  # type: ignore[attr-defined]

    # Verify active newline is automatically padded with standard 4-space block
    assert editor.toPlainText() == "def main_wrapper():\n    "
