"""Comprehensive test suite for Phase 4: Diagnostics Engine and Visual Pytest Runner.

Covers:
- Ruff in-memory diagnostics execution and JSON parsing
- Diagnostic auto-fix application coordinate mathematics
- Mypy static analysis output parsing
- CodeEditor wavy squiggles, tooltips, test detection, and gutter play triggers
- EditorTabs diagnostic forwarding and test execution routing
- PytestEngine test discovery and collection parsing
- TestRunnerPanel hierarchical suite tree, status badges, durations, filter, and tracebacks
- MainWindow integration and orchestration
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QTextEdit

from src import (
    AppConfig,
    ColorPalette,
    DiagnosticFix,
    DiagnosticIssue,
    EditLocation,
    EditorTheme,
    FixEdit,
    JediService,
    MypyService,
    PytestEngine,
    RuffService,
    TestItem,
    TestRunnerPanel,
    TestStatus,
    TestSuiteSummary,
    get_palette,
)
from src.app import MainWindow
from src.editor import CodeEditor, EditorTabs


@pytest.fixture
def palette() -> ColorPalette:
    return get_palette(EditorTheme.DARK)


@pytest.fixture
def jedi_service(qapp: QApplication) -> JediService:
    return JediService()


# -----------------------------------------------------------------------------
# Diagnostics Engine Tests
# -----------------------------------------------------------------------------


def test_ruff_service_json_parsing() -> None:
    """Verify Ruff JSON parser correctly extracts issues, ranges, and fixes."""
    service = RuffService()
    raw_json = """[
      {
        "code": "F401",
        "message": "`os` imported but unused",
        "severity": "error",
        "filename": "test_sample.py",
        "location": {"row": 1, "column": 8},
        "end_location": {"row": 1, "column": 10},
        "fix": {
          "message": "Remove unused import: `os`",
          "applicability": "safe",
          "edits": [
            {
              "content": "",
              "location": {"row": 1, "column": 1},
              "end_location": {"row": 2, "column": 1}
            }
          ]
        },
        "url": "https://docs.astral.sh/ruff/rules/unused-import"
      }
    ]"""

    issues = service._parse_ruff_json(raw_json, "default.py")
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "F401"
    assert issue.message == "`os` imported but unused"
    assert issue.severity == "error"
    assert issue.line == 1
    assert issue.column == 8
    assert issue.has_fix is True
    assert issue.fix_message == "Remove unused import: `os`"
    assert len(issue.fix.edits) == 1
    assert issue.fix.edits[0].content == ""


def test_ruff_service_check_source_real() -> None:
    """Verify check_source executes Ruff and identifies standard lint issues."""
    service = RuffService()
    code = "import os, sys\n\ndef add(a, b):\n    return a + b\n"
    issues = service.check_source(code, "sample.py")
    # If ruff is installed, F401 or I001 will be detected
    if service.find_executable():
        assert len(issues) >= 1
        codes = [iss.code for iss in issues]
        assert any("F401" in c or "I001" in c for c in codes)


def test_ruff_service_fallback_on_syntax_error() -> None:
    """Verify fallback parser produces a DiagnosticIssue on invalid Python syntax."""
    service = RuffService()
    broken_code = "def broken(\n    return 42"
    issues = service._fallback_ast_check(broken_code, "broken.py")
    assert len(issues) == 1
    assert issues[0].code == "E999"
    assert issues[0].severity == "error"


def test_ruff_service_apply_fix_reverse_order() -> None:
    """Verify applying diagnostic fixes accurately transforms source text."""
    source = "import os\n\ndef run():\n    return 1\n"
    issue = DiagnosticIssue(
        code="F401",
        message="unused import",
        severity="warning",
        file_path="sample.py",
        line=1,
        column=1,
        end_line=1,
        end_column=10,
        fix=DiagnosticFix(
            message="Remove unused import",
            applicability="safe",
            edits=[
                FixEdit(
                    content="",
                    location=EditLocation(row=1, column=1),
                    end_location=EditLocation(row=2, column=1),
                )
            ],
        ),
    )

    fixed = RuffService.apply_fix(source, issue)
    assert "import os" not in fixed
    assert "def run():" in fixed


def test_mypy_service_line_parsing() -> None:
    """Verify MypyService accurately parses standard compiler-style diagnostics."""
    service = MypyService()
    raw_mypy = "math_util.py:12:5: error: Incompatible types in assignment (expression has type \"str\", variable has type \"int\")  [assignment]\nmath_util.py:20:1: note: See details\n"
    target = Path("math_util.py")
    issues = service._parse_mypy_output(raw_mypy, target)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "assignment"
    assert issue.line == 12
    assert issue.column == 5
    assert issue.severity == "error"
    assert "Incompatible types" in issue.message


# -----------------------------------------------------------------------------
# CodeEditor Diagnostics & Gutter Triggers
# -----------------------------------------------------------------------------


def test_code_editor_diagnostic_squiggles(palette: ColorPalette, jedi_service: JediService) -> None:
    """Verify CodeEditor renders wavy squiggles when diagnostic issues are set."""
    editor = CodeEditor(palette, jedi_service)
    code = "import os\nimport sys\nx = 10\n"
    editor.setPlainText(code)

    issues = [
        DiagnosticIssue(
            code="F401",
            message="os imported but unused",
            severity="warning",
            line=1,
            column=8,
            end_line=1,
            end_column=10,
        ),
        DiagnosticIssue(
            code="F841",
            message="Local variable x is assigned to but never used",
            severity="error",
            line=3,
            column=1,
            end_line=3,
            end_column=2,
        ),
    ]

    editor.set_diagnostics(issues)
    assert len(editor.get_diagnostics()) == 2
    assert 1 in editor._diagnostic_issues_by_line
    assert 3 in editor._diagnostic_issues_by_line

    selections = editor._build_diagnostic_selections()
    assert len(selections) == 2


def test_code_editor_test_declaration_detection(palette: ColorPalette, jedi_service: JediService) -> None:
    """Verify CodeEditor scans and indexes test function and class declarations."""
    editor = CodeEditor(palette, jedi_service)
    code = (
        "def normal_func():\n"
        "    pass\n"
        "\n"
        "def test_addition():\n"
        "    assert 1 + 1 == 2\n"
        "\n"
        "class TestSuite:\n"
        "    def test_method(self):\n"
        "        pass\n"
    )
    editor.setPlainText(code)
    editor._scan_test_declarations()

    # Lines:
    # 1: def normal_func()
    # 4: def test_addition()
    # 7: class TestSuite
    # 8: def test_method()
    assert 4 in editor._test_declaration_lines
    assert editor._test_declaration_lines[4] == "test_addition"
    assert 7 in editor._test_declaration_lines
    assert editor._test_declaration_lines[7] == "TestSuite"
    assert 8 in editor._test_declaration_lines
    assert editor._test_declaration_lines[8] == "test_method"
    assert 1 not in editor._test_declaration_lines


def test_code_editor_gutter_click_runs_test(
    palette: ColorPalette, jedi_service: JediService, qtbot: object
) -> None:
    """Verify clicking the gutter margin on a test line emits test_run_requested."""
    editor = CodeEditor(palette, jedi_service)
    editor.set_file_path(Path("tests/test_demo.py"))
    code = "def test_alpha():\n    assert True\n"
    editor.setPlainText(code)
    editor._scan_test_declarations()
    editor.resize(600, 400)
    editor.show()

    received_signals: list[tuple[Any, str, int]] = []
    editor.test_run_requested.connect(
        lambda path, name, line: received_signals.append((path, name, line))
    )

    line_area = editor._line_number_area
    block = editor.document().findBlockByNumber(0)
    top_y = editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top()

    # Simulate mouse click at gutter coordinates
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(6.0, top_y + 6.0),
        QPointF(6.0, top_y + 6.0),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    line_area.mousePressEvent(event)

    assert len(received_signals) == 1
    path, name, line = received_signals[0]
    assert name == "test_alpha"
    assert line == 1


def test_code_editor_quick_fix_at_cursor(
    palette: ColorPalette, jedi_service: JediService
) -> None:
    """Verify apply_quick_fix_at_cursor executes the auto-fix and updates source."""
    editor = CodeEditor(palette, jedi_service)
    code = "import os\n\ndef greet():\n    pass\n"
    editor.setPlainText(code)

    issue = DiagnosticIssue(
        code="F401",
        message="unused import",
        line=1,
        column=1,
        end_line=1,
        end_column=10,
        fix=DiagnosticFix(
            message="Remove unused import",
            edits=[
                FixEdit(
                    content="",
                    location=EditLocation(row=1, column=1),
                    end_location=EditLocation(row=2, column=1),
                )
            ],
        ),
    )
    editor.set_diagnostics([issue])

    # Position cursor on line 1
    cursor = editor.textCursor()
    cursor.setPosition(0)
    editor.setTextCursor(cursor)

    applied = editor.apply_quick_fix_at_cursor()
    assert applied is True
    assert "import os" not in editor.toPlainText()


def test_editor_tabs_test_and_diagnostic_forwarding(
    palette: ColorPalette, jedi_service: JediService
) -> None:
    """Verify EditorTabs forwards test_run_requested and sets diagnostics on matching tab."""
    tabs = EditorTabs(palette, jedi_service)
    test_path = Path("tests/test_sample.py")
    editor = tabs.add_editor(test_path, "def test_foo():\n    pass\n")

    received = []
    tabs.test_run_requested.connect(lambda p, n, l: received.append((p, n, l)))

    editor.test_run_requested.emit(test_path, "test_foo", 1)
    assert len(received) == 1
    assert received[0][1] == "test_foo"

    # Set diagnostics via EditorTabs
    issue = DiagnosticIssue(
        code="W001", message="warning", line=1, column=1, end_line=1, end_column=5
    )
    tabs.set_diagnostics_for_path(test_path, [issue])
    assert len(editor.get_diagnostics()) == 1


# -----------------------------------------------------------------------------
# Pytest Engine & Test Runner Panel Tests
# -----------------------------------------------------------------------------


def test_pytest_engine_collection_parsing() -> None:
    """Verify PytestEngine parses collect-only output into TestItem models."""
    raw_collection = (
        "tests/test_editor.py::test_auto_indent\n"
        "tests/test_debugger.py::TestClass::test_method\n"
        "tests/test_panels.py::test_output\n"
    )

    items = PytestEngine.parse_collection_output(raw_collection)
    assert len(items) == 3

    assert items[0].node_id == "tests/test_editor.py::test_auto_indent"
    assert items[0].file_path == "tests/test_editor.py"
    assert items[0].test_name == "test_auto_indent"
    assert items[0].class_name is None

    assert items[1].node_id == "tests/test_debugger.py::TestClass::test_method"
    assert items[1].file_path == "tests/test_debugger.py"
    assert items[1].class_name == "TestClass"
    assert items[1].test_name == "test_method"


def test_test_runner_panel_hierarchy_and_status_updates(palette: ColorPalette) -> None:
    """Verify TestRunnerPanel populates hierarchy, updates badges, and formats durations."""
    panel = TestRunnerPanel(palette)
    items = [
        TestItem(
            node_id="tests/test_a.py::test_one",
            file_path="tests/test_a.py",
            test_name="test_one",
        ),
        TestItem(
            node_id="tests/test_a.py::test_two",
            file_path="tests/test_a.py",
            test_name="test_two",
        ),
        TestItem(
            node_id="tests/test_b.py::Suite::test_three",
            file_path="tests/test_b.py",
            class_name="Suite",
            test_name="test_three",
        ),
    ]

    panel.set_tests(items)
    assert panel._tree.topLevelItemCount() == 2  # test_a.py and test_b.py

    # Update statuses
    panel.update_test_status("tests/test_a.py::test_one", TestStatus.PASSED, duration_ms=45.2)
    panel.update_test_status(
        "tests/test_a.py::test_two",
        TestStatus.FAILED,
        duration_ms=120.0,
        traceback="AssertionError: assert 1 == 2",
    )

    # Check that tree item reflects status
    item_one_tree = panel._tree_items_by_node_id["tests/test_a.py::test_one"]
    assert "Passed" in item_one_tree.text(1)
    assert "45ms" in item_one_tree.text(2)

    item_two_tree = panel._tree_items_by_node_id["tests/test_a.py::test_two"]
    assert "Failed" in item_two_tree.text(1)

    # Verify summary banner update
    summary = TestSuiteSummary(total=3, passed=1, failed=1, skipped=0, duration_sec=0.25)
    panel.set_summary(summary)
    assert "1 Passed" in panel._summary_label.text()
    assert "1 Failed" in panel._summary_label.text()


def test_test_runner_panel_filtering(palette: ColorPalette) -> None:
    """Verify text search dynamically filters the test suite tree."""
    panel = TestRunnerPanel(palette)
    items = [
        TestItem(node_id="tests/test_alpha.py::test_foo", file_path="tests/test_alpha.py", test_name="test_foo"),
        TestItem(node_id="tests/test_beta.py::test_bar", file_path="tests/test_beta.py", test_name="test_bar"),
    ]
    panel.set_tests(items)

    # Filter for 'foo'
    panel._apply_filter("foo")
    alpha_item = panel._tree.topLevelItem(0)
    beta_item = panel._tree.topLevelItem(1)

    assert not alpha_item.isHidden()
    assert beta_item.isHidden()

    # Clear filter
    panel._apply_filter("")
    assert not alpha_item.isHidden()
    assert not beta_item.isHidden()


def test_test_runner_panel_detail_and_traceback_viewer(palette: ColorPalette) -> None:
    """Verify clicking a test displays its failure traceback in the detail pane."""
    panel = TestRunnerPanel(palette)
    item = TestItem(
        node_id="tests/test_fail.py::test_boom",
        file_path="tests/test_fail.py",
        test_name="test_boom",
        line_number=15,
        status=TestStatus.FAILED,
        traceback="ZeroDivisionError: division by zero",
    )
    panel.set_tests([item])

    tree_item = panel._tree_items_by_node_id["tests/test_fail.py::test_boom"]
    panel._on_item_clicked(tree_item, 0)

    assert "ZeroDivisionError" in panel._traceback_edit.toPlainText()
    assert panel._btn_jump.isEnabled() is True


# -----------------------------------------------------------------------------
# MainWindow Integration Tests
# -----------------------------------------------------------------------------


def test_main_window_phase4_orchestration(tmp_path: Path, qtbot: object) -> None:
    """Verify MainWindow initializes TestRunnerPanel, Ruff, Mypy, and wires all shortcuts."""
    config = AppConfig(log_directory=tmp_path / "logs")
    window = MainWindow(config)

    # Verify panel and engine instantiations
    assert hasattr(window, "_test_panel")
    assert isinstance(window._test_panel, TestRunnerPanel)
    assert hasattr(window, "_pytest_engine")
    assert isinstance(window._pytest_engine, PytestEngine)
    assert hasattr(window, "_ruff_service")
    assert isinstance(window._ruff_service, RuffService)
    assert hasattr(window, "_mypy_service")
    assert isinstance(window._mypy_service, MypyService)

    # Verify bottom tab registration
    tab_labels = [window._bottom_tabs.tabText(i) for i in range(window._bottom_tabs.count())]
    assert "🧪 Tests" in tab_labels

    # Verify gutter test run request switches to tests panel
    window._on_gutter_test_run_requested(Path("tests/test_editor.py"), "test_auto_indent", 10)
    assert window._bottom_tabs.currentWidget() == window._test_panel

    window.close()
