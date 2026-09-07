"""Unit tests for Editor Gutter Git Diffs and Hunk Reverting."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint
from PySide6.QtGui import QPaintEvent
from PySide6.QtWidgets import QApplication

from src.editor import CodeEditor, DiffHunkPopup, EditorTabs
from src.pip_viper import ColorPalette, EditorTheme, JediService, get_palette
from src.vcs import GitDiffHunk, GitDiffType


def test_code_editor_set_git_diff_hunks(qtbot: Any) -> None:
    """Verify CodeEditor maps diff hunks across affected line numbers."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    editor = CodeEditor(palette, jedi)
    getattr(qtbot, "addWidget")(editor)

    hunks = [
        GitDiffHunk(
            diff_type=GitDiffType.MODIFIED,
            start_line=2,
            line_count=3,
            original_content="old lines",
            modified_content="new lines",
        ),
        GitDiffHunk(
            diff_type=GitDiffType.ADDED,
            start_line=10,
            line_count=1,
            original_content="",
            modified_content="added line",
        ),
        GitDiffHunk(
            diff_type=GitDiffType.DELETED,
            start_line=15,
            line_count=1,
            original_content="deleted line",
            modified_content="",
        ),
    ]

    editor.set_git_diff_hunks(hunks)
    assert len(editor.git_diff_hunks()) == 3

    # Lines 2, 3, 4 map to hunk 0
    assert editor._git_diff_by_line.get(2) == hunks[0]
    assert editor._git_diff_by_line.get(3) == hunks[0]
    assert editor._git_diff_by_line.get(4) == hunks[0]
    assert editor._git_diff_by_line.get(5) is None

    # Line 10 maps to hunk 1
    assert editor._git_diff_by_line.get(10) == hunks[1]

    # Line 15 maps to hunk 2
    assert editor._git_diff_by_line.get(15) == hunks[2]


def test_code_editor_revert_modified_hunk(qtbot: Any) -> None:
    """Verify reverting a modified hunk restores the original content."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    editor = CodeEditor(palette, jedi)
    getattr(qtbot, "addWidget")(editor)

    initial_text = "line 1\nMODIFIED LINE 2\nline 3\n"
    editor.setPlainText(initial_text)

    hunk = GitDiffHunk(
        diff_type=GitDiffType.MODIFIED,
        start_line=2,
        line_count=1,
        original_content="original line 2",
        modified_content="MODIFIED LINE 2",
    )

    editor.revert_git_diff_hunk(hunk)
    assert "original line 2" in editor.toPlainText()
    assert "MODIFIED LINE 2" not in editor.toPlainText()


def test_code_editor_revert_added_hunk(qtbot: Any) -> None:
    """Verify reverting an added hunk deletes the inserted lines."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    editor = CodeEditor(palette, jedi)
    getattr(qtbot, "addWidget")(editor)

    initial_text = "line 1\nEXTRA LINE\nline 2\n"
    editor.setPlainText(initial_text)

    hunk = GitDiffHunk(
        diff_type=GitDiffType.ADDED,
        start_line=2,
        line_count=1,
        original_content="",
        modified_content="EXTRA LINE",
    )

    editor.revert_git_diff_hunk(hunk)
    assert "EXTRA LINE" not in editor.toPlainText()
    assert "line 1\nline 2" in editor.toPlainText()


def test_code_editor_revert_deleted_hunk(qtbot: Any) -> None:
    """Verify reverting a deleted hunk restores the deleted text."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    editor = CodeEditor(palette, jedi)
    getattr(qtbot, "addWidget")(editor)

    initial_text = "line 1\nline 3\n"
    editor.setPlainText(initial_text)

    hunk = GitDiffHunk(
        diff_type=GitDiffType.DELETED,
        start_line=2,
        line_count=1,
        original_content="line 2",
        modified_content="",
    )

    editor.revert_git_diff_hunk(hunk)
    assert "line 2" in editor.toPlainText()


def test_diff_hunk_popup_action(qtbot: Any) -> None:
    """Verify DiffHunkPopup renders and triggers hunk revert."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    editor = CodeEditor(palette, jedi)
    getattr(qtbot, "addWidget")(editor)

    editor.setPlainText("def add(a, b):\n    return a - b\n")

    hunk = GitDiffHunk(
        diff_type=GitDiffType.MODIFIED,
        start_line=2,
        line_count=1,
        original_content="    return a + b",
        modified_content="    return a - b",
    )

    popup = DiffHunkPopup(editor, hunk, editor, QPoint(100, 100))
    getattr(qtbot, "addWidget")(popup)

    # Click revert inside popup
    popup._on_revert()
    assert "return a + b" in editor.toPlainText()


def test_editor_tabs_set_git_diff_for_path(qtbot: Any, tmp_path: Path) -> None:
    """Verify EditorTabs distributes git diff hunks to matching open editors."""
    palette = get_palette(EditorTheme.DARK)
    jedi = JediService()
    tabs = EditorTabs(palette, jedi)
    getattr(qtbot, "addWidget")(tabs)

    test_file = tmp_path / "calc.py"
    test_file.write_text("print('calc')\n", encoding="utf-8")

    editor = tabs.add_editor(test_file, "print('calc')\n")
    assert len(editor.git_diff_hunks()) == 0

    hunks = [
        GitDiffHunk(
            diff_type=GitDiffType.ADDED,
            start_line=1,
            line_count=1,
            original_content="",
            modified_content="print('calc')",
        )
    ]
    tabs.set_git_diff_for_path(test_file, hunks)
    assert len(editor.git_diff_hunks()) == 1
