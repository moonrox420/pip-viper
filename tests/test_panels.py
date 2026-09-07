"""Unit tests for bottom dock panels and control widgets."""

from __future__ import annotations

from pathlib import Path

from src import (
    ColorPalette,
    EditorTheme,
    LintIssue,
    LintSeverity,
    LintTool,
    PipPackage,
    get_palette,
)
from src.code_tools import CodeIssue, FixSeverity
from src.panels import (
    CodeToolsPanel,
    DebugPanel,
    LintWidget,
    LogPanel,
    LogWidget,
    OutputPanel,
    PackageManagerWidget,
)


def test_output_panel_streams_and_clear(qtbot: object) -> None:
    """Verify OutputPanel appends to stdout/stderr and clears cleanly."""
    palette = get_palette(EditorTheme.DARK)
    panel = OutputPanel(palette)
    getattr(qtbot, "addWidget")(panel)

    panel.append_stdout("Standard test log message")
    panel.append_stderr("Standard test error message")

    assert "Standard test log message" in panel._stdout_view.toPlainText()
    assert "Standard test error message" in panel._stderr_view.toPlainText()

    panel.clear()
    assert panel._stdout_view.toPlainText() == ""
    assert panel._stderr_view.toPlainText() == ""


def test_output_panel_set_palette(qtbot: object) -> None:
    """Verify theme palette updates cleanly apply to OutputPanel."""
    palette_dark = get_palette(EditorTheme.DARK)
    panel = OutputPanel(palette_dark)
    getattr(qtbot, "addWidget")(panel)

    palette_light = get_palette(EditorTheme.LIGHT)
    panel.set_palette(palette_light)
    assert palette_light.background in panel._stdout_view.styleSheet()


def test_code_tools_panel_busy_and_diagnostics(qtbot: object) -> None:
    """Verify CodeToolsPanel toggles busy status and populates issues."""
    palette = get_palette(EditorTheme.DARK)
    panel = CodeToolsPanel(palette)
    getattr(qtbot, "addWidget")(panel)

    # Status indicator updates
    panel.set_busy(True)
    assert "PROCESSING" in panel._status_label.text()
    assert not panel._action_buttons[0].isEnabled()

    panel.set_busy(False)
    assert "IDLE" in panel._status_label.text()
    assert panel._action_buttons[0].isEnabled()

    # Issue population
    issues = [
        CodeIssue(line=5, column=2, message="Syntax error: invalid syntax", severity=FixSeverity.ERROR),
        CodeIssue(line=12, column=0, message="Indentation warning", severity=FixSeverity.WARNING),
    ]
    panel.populate_issues(issues)
    assert panel._results_table.rowCount() == 2
    assert panel._results_table.item(0, 0).text() == "5"
    assert panel._results_table.item(0, 2).text() == "Syntax error: invalid syntax"

    # Clean report when no issues detected
    panel.populate_issues([])
    assert panel._results_table.rowCount() == 1
    assert "No syntax issues" in panel._results_table.item(0, 2).text()

    panel.clear_issues()
    assert panel._results_table.rowCount() == 0


def test_lint_widget_populate_and_clear(qtbot: object) -> None:
    """Verify LintWidget displays static analysis issues and clears."""
    palette = get_palette(EditorTheme.DARK)
    widget = LintWidget(palette)
    getattr(qtbot, "addWidget")(widget)

    issues = [
        LintIssue(
            file_path=Path("app.py"),
            line=10,
            column=4,
            code="E501",
            tool=LintTool.FLAKE8,
            severity=LintSeverity.WARNING,
            message="line too long",
        ),
        LintIssue(
            file_path=Path("app.py"),
            line=25,
            column=0,
            code="E0602",
            tool=LintTool.PYLINT,
            severity=LintSeverity.ERROR,
            message="undefined variable 'x'",
        ),
    ]
    widget.populate(issues)
    assert widget._table.rowCount() == 2
    assert "2 issue(s)" in widget._summary_label.text()

    widget._clear()
    assert widget._table.rowCount() == 0
    assert widget._summary_label.text() == "No lint results yet."


def test_log_widget_file_loading(qtbot: object, tmp_path: Path) -> None:
    """Verify LogWidget loads text from an active log file."""
    log_file = tmp_path / "test_run.log"
    log_file.write_text("2026-09-06 [trace123] INFO main :: system initialized\n", encoding="utf-8")

    widget = LogWidget()
    getattr(qtbot, "addWidget")(widget)

    widget.load_file(log_file)
    assert "system initialized" in widget.toPlainText()

    # Non-existent file handled gracefully
    missing_file = tmp_path / "missing.log"
    widget.load_file(missing_file)
    assert "no log file yet" in widget.toPlainText()


def test_package_manager_widget_filtering(qtbot: object) -> None:
    """Verify PackageManagerWidget renders and filters packages by name."""
    palette = get_palette(EditorTheme.DARK)
    widget = PackageManagerWidget(palette, python_executable="python")
    getattr(qtbot, "addWidget")(widget)

    packages = [
        PipPackage(name="pytest", version="9.1.1", location=Path("/site-packages")),
        PipPackage(name="pyside6", version="6.11.2", location=Path("/site-packages")),
        PipPackage(name="black", version="26.5.1", location=Path("/site-packages")),
    ]
    widget.populate(packages)
    assert widget._table.rowCount() == 3

    # Filter for 'py' should keep pytest and pyside6
    widget._apply_filter("py")
    assert widget._table.rowCount() == 2

    # Clear filter restores full list
    widget._apply_filter("")
    assert widget._table.rowCount() == 3


def test_debug_panel_state_and_logging(qtbot: object) -> None:
    """Verify DebugPanel tracks session state and logs messages."""
    palette = get_palette(EditorTheme.DARK)
    panel = DebugPanel(palette)
    getattr(qtbot, "addWidget")(panel)

    panel.set_session_active(True, port=5678)
    assert "ACTIVE (port 5678)" in panel._status_label.text()

    panel.log_message("Listening for debugger client connection...")
    assert "Listening for debugger client" in panel._console.toPlainText()

    panel.set_session_active(False)
    assert "Inactive" in panel._status_label.text()

    panel.clear_console()
    assert panel._console.toPlainText() == ""


def test_git_panel_rendering_and_status(qtbot: object) -> None:
    """Verify GitPanel populates staged and unstaged trees and handles logging."""
    from src.panels import GitPanel, GitStatusEntry, GitFileStatus

    palette = get_palette(EditorTheme.DARK)
    panel = GitPanel(palette)
    getattr(qtbot, "addWidget")(panel)

    panel._staged_entries = [
        GitStatusEntry(file_path=Path("src/app.py"), status=GitFileStatus.MODIFIED, staged=True),
        GitStatusEntry(file_path=Path("src/new.py"), status=GitFileStatus.ADDED, staged=True),
    ]
    panel._unstaged_entries = [
        GitStatusEntry(file_path=Path("tests/test_vcs.py"), status=GitFileStatus.UNTRACKED, staged=False),
    ]
    panel._repopulate_trees()

    assert panel._staged_tree.topLevelItemCount() == 2
    assert panel._unstaged_tree.topLevelItemCount() == 1
    assert "Staged Changes (2)" in panel._staged_group.title()
    assert "Changes (1)" in panel._unstaged_group.title()

    panel._append("Switched to branch feature-test")
    assert "Switched to branch feature-test" in panel._output_view.toPlainText()

