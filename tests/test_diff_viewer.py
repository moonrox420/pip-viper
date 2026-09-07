"""Unit tests for the Visual Diff Viewer and Dialog."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QApplication

from src.diff_viewer import DiffViewerDialog, DiffViewerWidget
from src.pip_viper import EditorTheme, get_palette


def test_diff_viewer_widget_population(qtbot: Any) -> None:
    """Verify DiffViewerWidget parses and populates additions and deletions."""
    palette = get_palette(EditorTheme.DARK)
    old_text = "line1\nline2\nline3\n"
    new_text = "line1\nline2_modified\nline3\nline4\n"

    widget = DiffViewerWidget(
        palette=palette,
        file_path=Path("test.py"),
        old_text=old_text,
        new_text=new_text,
        is_staged=False,
    )
    getattr(qtbot, "addWidget")(widget)

    # 4 lines total in diff
    assert widget._unified_table.rowCount() == 5  # line1, -line2, +line2_modified, line3, +line4
    assert "+2" in widget._stats_label.text()
    assert "-1" in widget._stats_label.text()


def test_diff_viewer_mode_toggle(qtbot: Any) -> None:
    """Verify switching between Unified and Side-by-Side modes."""
    palette = get_palette(EditorTheme.DARK)
    widget = DiffViewerWidget(
        palette=palette,
        file_path=Path("demo.py"),
        old_text="a\nb\n",
        new_text="a\nB\n",
    )
    getattr(qtbot, "addWidget")(widget)

    # Default is unified
    assert not widget._unified_table.isHidden()
    assert widget._side_splitter.isHidden()

    # Switch to side-by-side
    widget._btn_side.click()
    assert widget._unified_table.isHidden()
    assert not widget._side_splitter.isHidden()
    assert widget._left_table.rowCount() > 0
    assert widget._right_table.rowCount() > 0

    # Switch back to unified
    widget._btn_unified.click()
    assert not widget._unified_table.isHidden()
    assert widget._side_splitter.isHidden()


def test_diff_viewer_dialog_signals(qtbot: Any) -> None:
    """Verify DiffViewerDialog forwards action signals."""
    palette = get_palette(EditorTheme.DARK)
    test_path = Path("src/module.py")

    dialog = DiffViewerDialog(
        palette=palette,
        file_path=test_path,
        old_text="old",
        new_text="new",
        is_staged=False,
    )
    getattr(qtbot, "addWidget")(dialog)

    staged_paths: list[Path] = []
    discarded_paths: list[Path] = []
    dialog.stage_requested.connect(staged_paths.append)
    dialog.discard_requested.connect(discarded_paths.append)

    # Click Stage
    dialog._viewer._action_btn.click()
    assert len(staged_paths) == 1
    assert staged_paths[0] == test_path

    # Click Discard
    dialog._viewer._discard_btn.click()
    assert len(discarded_paths) == 1
    assert discarded_paths[0] == test_path
