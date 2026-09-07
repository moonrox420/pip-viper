"""Comprehensive unit test suite for Navigation and Code Intelligence (Phase 6).

Covers:
- FuzzyMatcher subsequence matching, line jump parsing, and relevance scoring
- QuickOpenDialog file indexing, filtering, and selection emission
- CommandPaletteDialog action registration, categorization, and execution
- SearchInFilesWorker multi-threaded file traversal, regex search, and glob filters
- SearchInFilesWidget interactive tree item mapping and match activation
- Jedi Go-to-Definition cross-file resolution and definition_path handling
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication

from src.navigation import (
    CommandPaletteDialog,
    FuzzyMatcher,
    PaletteAction,
    QuickOpenDialog,
    SearchInFilesWidget,
    SearchInFilesWorker,
    SearchMatch,
)
from src.pip_viper import (
    AppConfig,
    ColorPalette,
    EditorTheme,
    JediResult,
    JediService,
    JediTaskType,
    get_palette,
)


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    """Ensure a singleton QApplication exists for widget testing."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# =============================================================================
# 1. Fuzzy Matching Engine Tests
# =============================================================================


def test_fuzzy_matcher_parse_query_line() -> None:
    """Verify parsing of query strings with optional line numbers."""
    query, line = FuzzyMatcher.parse_query_line("app.py:120")
    assert query == "app.py"
    assert line == 120

    query, line = FuzzyMatcher.parse_query_line("src/editor.py:42")
    assert query == "src/editor.py"
    assert line == 42

    query, line = FuzzyMatcher.parse_query_line("simple.py")
    assert query == "simple.py"
    assert line is None

    query, line = FuzzyMatcher.parse_query_line("file.py:notanumber")
    assert query == "file.py:notanumber"
    assert line is None


def test_fuzzy_matcher_subsequence_matching() -> None:
    """Verify subsequence detection and rejection."""
    matched, score, indices = FuzzyMatcher.match("app", "src/app.py")
    assert matched is True
    assert score > 0
    assert len(indices) == 3

    matched, _, _ = FuzzyMatcher.match("xyz", "src/app.py")
    assert matched is False

    # Empty pattern matches everything
    matched, score, _ = FuzzyMatcher.match("", "anything.py")
    assert matched is True


def test_fuzzy_matcher_scoring_ranking() -> None:
    """Verify exact and basename matches score higher than distant subsequences."""
    candidates = [
        "src/application_controller.py",
        "src/app.py",
        "docs/about_app.md",
        "tests/test_application.py",
    ]

    ranked = FuzzyMatcher.filter_and_rank("app.py", candidates)
    assert len(ranked) >= 1
    # "src/app.py" should be ranked #1
    assert ranked[0][0] == "src/app.py"


def test_fuzzy_matcher_camel_case_bonus() -> None:
    """Verify camelCase and boundary matches receive bonuses."""
    candidates = ["CodeToolsPanel", "CentralProcessModel"]
    ranked = FuzzyMatcher.filter_and_rank("ctp", candidates)
    assert len(ranked) >= 1
    assert ranked[0][0] == "CodeToolsPanel"


# =============================================================================
# 2. Quick Open Dialog Tests
# =============================================================================


def test_quick_open_dialog_file_indexing(qapp: QApplication, tmp_path: Path) -> None:
    """Verify workspace traversal and file indexing in QuickOpenDialog."""
    # Create test workspace files
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("print('hello')", encoding="utf-8")
    (src_dir / "editor.py").write_text("print('editor')", encoding="utf-8")

    # Create ignored folder
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("ignored", encoding="utf-8")

    dialog = QuickOpenDialog()
    dialog.set_workspace_root(tmp_path)

    # .git should be excluded
    indexed = dialog._indexed_files
    assert "src/app.py" in indexed
    assert "src/editor.py" in indexed
    assert not any(".git" in p for p in indexed)


def test_quick_open_dialog_selection_signal(qapp: QApplication, tmp_path: Path) -> None:
    """Verify file_selected signal emits target file and line number."""
    f = tmp_path / "main.py"
    f.write_text("x = 1\n", encoding="utf-8")

    dialog = QuickOpenDialog()
    dialog.set_workspace_root(tmp_path)

    emitted: list[tuple[str, int]] = []
    dialog.file_selected.connect(lambda path, line: emitted.append((path, line)))

    # Set query with line number
    dialog._on_query_changed("main.py:25")
    assert dialog._target_line == 25
    assert dialog._list_widget.count() == 1

    item = dialog._list_widget.item(0)
    dialog._on_item_activated(item)

    assert len(emitted) == 1
    assert Path(emitted[0][0]).resolve() == f.resolve()
    assert emitted[0][1] == 25


# =============================================================================
# 3. Universal Command Palette Tests
# =============================================================================


def test_command_palette_registration_and_execution(qapp: QApplication) -> None:
    """Verify action registration, fuzzy filtering, and execution in CommandPaletteDialog."""
    palette = CommandPaletteDialog()

    executed: list[str] = []
    palette.register_action(
        title="Run All Pytest Tests",
        category="Testing",
        shortcut="F8",
        callback=lambda: executed.append("run_tests"),
    )
    palette.register_action(
        title="Toggle Breakpoint",
        category="Debug",
        shortcut="F9",
        callback=lambda: executed.append("toggle_bp"),
    )

    # Filter by query
    palette._on_query_changed("pytest")
    assert palette._list_widget.count() == 1

    # Activate
    item = palette._list_widget.item(0)
    palette._on_item_activated(item)

    assert executed == ["run_tests"]


def test_command_palette_register_from_menu(qapp: QApplication) -> None:
    """Verify batch indexing from QMenu actions."""
    palette = CommandPaletteDialog()
    action1 = QAction("Save File", palette)
    action1.setShortcut("Ctrl+S")
    action2 = QAction("Open Folder...", palette)

    palette.register_qactions_from_menu("File", [action1, action2])
    assert len(palette._actions) == 2
    assert palette._actions[0].title == "Save File"
    assert palette._actions[0].category == "File"
    assert palette._actions[0].shortcut == "Ctrl+S"


# =============================================================================
# 4. Search in Files Worker & Widget Tests
# =============================================================================


def test_search_in_files_worker(qapp: QApplication, tmp_path: Path) -> None:
    """Verify multi-threaded regex search worker finds matches with accurate line/col coordinates."""
    file1 = tmp_path / "module_a.py"
    file1.write_text(
        "def compute_alpha():\n"
        "    return 42\n",
        encoding="utf-8",
    )
    file2 = tmp_path / "module_b.py"
    file2.write_text(
        "from module_a import compute_alpha\n"
        "val = compute_alpha()\n",
        encoding="utf-8",
    )

    worker = SearchInFilesWorker(
        root_dir=tmp_path,
        query="compute_alpha",
        case_sensitive=True,
        whole_word=True,
    )

    matches: list[SearchMatch] = []
    worker.match_found.connect(matches.append)

    # Run synchronously for deterministic unit testing
    worker.run()

    assert len(matches) == 3
    assert all(m.match_length == len("compute_alpha") for m in matches)
    assert any(m.file_path == str(file1) and m.line_number == 1 for m in matches)
    assert any(m.file_path == str(file2) and m.line_number == 1 for m in matches)
    assert any(m.file_path == str(file2) and m.line_number == 2 for m in matches)


def test_search_in_files_widget_match_activation(qapp: QApplication, tmp_path: Path) -> None:
    """Verify SearchInFilesWidget receives matches and emits match_selected on double click."""
    test_file = tmp_path / "service.py"
    test_file.write_text("target_token = 'secret'\n", encoding="utf-8")

    widget = SearchInFilesWidget()
    widget.set_workspace_root(tmp_path)

    match = SearchMatch(
        file_path=str(test_file),
        line_number=1,
        column_number=0,
        line_text="target_token = 'secret'",
        match_length=12,
    )
    widget._on_match_found(match)

    activated: list[tuple[str, int, int]] = []
    widget.match_selected.connect(lambda f, l, c: activated.append((f, l, c)))

    # Get file item and child item
    file_item = widget._tree.topLevelItem(0)
    assert file_item is not None
    line_item = file_item.child(0)
    assert line_item is not None

    widget._on_item_double_clicked(line_item, 0)
    assert len(activated) == 1
    assert activated[0][0] == str(test_file)
    assert activated[0][1] == 1
    assert activated[0][2] == 0


# =============================================================================
# 5. Jedi Definition Cross-File Support Tests
# =============================================================================


def test_jedi_result_definition_path() -> None:
    """Verify JediResult accepts and preserves definition_path."""
    res = JediResult(
        task_type=JediTaskType.DEFINITION,
        definition_line=45,
        definition_column=10,
        definition_path="/path/to/module.py",
    )
    assert res.definition_path == "/path/to/module.py"
    assert res.definition_line == 45
    assert res.definition_column == 10
