"""Unit tests for production hardening, packaging validation, and InternalsPanel upgrade."""

from __future__ import annotations

import logging
from pathlib import Path
import sys

import pytest

from desktop_packaging.build_standalone import smoke_test_modules, validate_environment, verify_spec_file
from src.app import install_global_exception_handler
from src.internals import ExecutionProfiler, ProfileResult
from src.panels import InternalsPanel
from src.pip_viper import EditorTheme, get_palette


def test_global_exception_handler(caplog: pytest.LogCaptureFixture) -> None:
    """Verify global exception handler wraps sys.excepthook and logs critical crashes."""
    original_hook = sys.excepthook
    try:
        install_global_exception_handler()
        assert sys.excepthook != original_hook

        # Simulate exception passed to hook
        with caplog.at_level(logging.CRITICAL):
            try:
                raise ValueError("Simulated unhandled runtime error")
            except ValueError as e:
                sys.excepthook(type(e), e, e.__traceback__)

        assert "Unhandled application exception" in caplog.text
        assert "Simulated unhandled runtime error" in caplog.text
    finally:
        sys.excepthook = original_hook


def test_packaging_spec_file() -> None:
    """Verify PyInstaller specification file exists and contains core build instructions."""
    spec_path = Path("desktop_packaging/pip_viper.spec")
    assert verify_spec_file(spec_path)

    spec_content = spec_path.read_text(encoding="utf-8")
    assert "Analysis(" in spec_content
    assert "PYZ(" in spec_content
    assert "EXE(" in spec_content
    assert "COLLECT(" in spec_content
    assert "launcher.py" in spec_content
    assert "console=False" in spec_content


def test_packaging_build_standalone_module() -> None:
    """Verify packaging validation and smoke test utilities."""
    ok, issues = validate_environment()
    assert ok is True
    assert len(issues) == 0

    smoke_results = smoke_test_modules()
    assert len(smoke_results) > 0
    assert all(success for _, success, _ in smoke_results)


def test_internals_panel_multi_tab_profiler(qtbot: object) -> None:
    """Verify InternalsPanel hosts the 5-in-1 multi-view execution & memory studio."""
    palette = get_palette(EditorTheme.DARK)
    panel = InternalsPanel(palette)
    getattr(qtbot, "addWidget")(panel)

    # Check that Profiler sub-tabs exist
    assert hasattr(panel, "_prof_subtabs")
    assert panel._prof_subtabs.count() == 5
    assert panel._prof_subtabs.tabText(0) == "🔥 Flame Graph"
    assert panel._prof_subtabs.tabText(1) == "🌳 Call Hierarchy"
    assert panel._prof_subtabs.tabText(2) == "⚡ Hotspots Table"
    assert panel._prof_subtabs.tabText(3) == "💾 Memory & Leaks"
    assert panel._prof_subtabs.tabText(4) == "📃 Output"

    # Profile sample code and populate panel
    sample_code = "def foo(): return [x * 2 for x in range(1000)]\nfoo()\n"
    res = ExecutionProfiler.profile_code(sample_code, "test_sample.py")
    assert res.root_call_node is not None

    panel.populate_profile(res)

    # Ensure flame graph and call hierarchy received data
    assert panel._flame_graph._root_node is not None
    assert panel._call_hierarchy._func_tree.topLevelItemCount() > 0
    assert panel._profile_table.rowCount() > 0

    # Test clear_profiler
    panel.clear_profiler()
    assert panel._profile_table.rowCount() == 0
    assert panel._profile_status.text() == "Profiler: IDLE"
