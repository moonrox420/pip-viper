"""Unit tests for FileExplorer and DocumentOutlineWidget."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt

from src.widgets import DocumentOutlineWidget, FileExplorer


def test_file_explorer_initialization_and_set_root(qtbot: object, tmp_path: Path) -> None:
    """Verify FileExplorer initializes and correctly updates its root directory."""
    explorer = FileExplorer()
    getattr(qtbot, "addWidget")(explorer)

    explorer.set_root(tmp_path)
    assert explorer._header_label.text() == f"WORKSPACE: {tmp_path.name or str(tmp_path)}"


def test_file_explorer_filter_patterns(qtbot: object) -> None:
    """Verify filename filtering updates the underlying filesystem model filters."""
    explorer = FileExplorer()
    getattr(qtbot, "addWidget")(explorer)

    # Empty filter clears filters
    explorer._on_filter_changed("")
    assert explorer._model.nameFilters() == []

    # Single extension adds wildcards
    explorer._on_filter_changed("py")
    assert explorer._model.nameFilters() == ["*py*"]

    # Explicit glob pattern preserved
    explorer._on_filter_changed("*.py, test_*.py")
    assert "*.py" in explorer._model.nameFilters()
    assert "test_*.py" in explorer._model.nameFilters()


def test_file_explorer_file_activation(qtbot: object, tmp_path: Path) -> None:
    """Verify clicking/activating a file emits the file_activated signal."""
    test_file = tmp_path / "hello.py"
    test_file.write_text("print('hello')", encoding="utf-8")

    explorer = FileExplorer()
    getattr(qtbot, "addWidget")(explorer)
    explorer.set_root(tmp_path)

    activated_paths: list[Path] = []
    explorer.file_activated.connect(activated_paths.append)

    index = explorer._model.index(str(test_file))
    explorer._on_activated(index)

    assert len(activated_paths) == 1
    assert activated_paths[0] == test_file


def test_outline_empty_source_shows_hint(qtbot: object) -> None:
    """An empty source code string should display the placeholder hint."""
    outline = DocumentOutlineWidget()
    getattr(qtbot, "addWidget")(outline)
    outline.show()

    outline.update_from_source("")
    assert outline._hint_label.isVisible()
    assert outline._list.isHidden()


def test_outline_syntax_error_shows_explanatory_hint(qtbot: object) -> None:
    """Broken syntax displays an outline unavailable message."""
    outline = DocumentOutlineWidget()
    getattr(qtbot, "addWidget")(outline)
    outline.show()

    outline.update_from_source("def broken_func(:\n    pass\n")
    assert outline._hint_label.isVisible()
    assert "fix the syntax error" in outline._hint_label.text().lower()


def test_outline_populates_classes_functions_and_methods(qtbot: object) -> None:
    """Valid classes, methods, and functions should populate with appropriate depth."""
    source = (
        "class MyService:\n"
        "    def sync_method(self) -> None:\n"
        "        pass\n"
        "    async def async_method(self) -> None:\n"
        "        pass\n"
        "\n"
        "def top_func() -> None:\n"
        "    pass\n"
        "\n"
        "async def async_top_func() -> None:\n"
        "    pass\n"
    )

    outline = DocumentOutlineWidget()
    getattr(qtbot, "addWidget")(outline)
    outline.show()

    outline.update_from_source(source)
    assert outline._list.isVisible()
    assert outline._hint_label.isHidden()
    assert outline._list.count() == 5

    items_text = [outline._list.item(i).text() for i in range(outline._list.count())]
    assert any("MyService" in t for t in items_text)
    assert any("sync_method()" in t for t in items_text)
    assert any("async_method()" in t for t in items_text)
    assert any("top_func()" in t for t in items_text)
    assert any("async_top_func()" in t for t in items_text)


def test_outline_item_click_emits_selected_signal(qtbot: object) -> None:
    """Clicking an outline entry emits its line and column coordinates."""
    source = "def example_function():\n    pass\n"

    outline = DocumentOutlineWidget()
    getattr(qtbot, "addWidget")(outline)

    outline.update_from_source(source)
    assert outline._list.count() == 1

    selected_coords: list[tuple[int, int]] = []
    outline.item_selected.connect(lambda line, col: selected_coords.append((line, col)))

    item = outline._list.item(0)
    outline._on_clicked(item)

    assert len(selected_coords) == 1
    line, col = selected_coords[0]
    assert line == 1
    assert col == 0
