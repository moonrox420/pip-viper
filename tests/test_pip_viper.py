"""Tests for PipViper core configurations, structured logging, and background task execution.

This module provides thorough unit and integration tests for the PipViper
subsystems using the pytest framework and pytest-qt for async signals.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import (
    AppConfig,
    ColorPalette,
    EditorTheme,
    JediResult,
    JediService,
    JediTaskType,
    get_palette,
    run_in_thread,
)


def test_app_config_validation() -> None:
    """Verify that AppConfig validates user inputs correctly using Pydantic."""
    config = AppConfig(font_size=14, auto_save_seconds=60)
    assert config.font_size == 14
    assert config.auto_save_seconds == 60

    # Pydantic should catch value out of bounds
    with pytest.raises(ValueError):
        AppConfig(font_size=100)

    with pytest.raises(ValueError):
        AppConfig(auto_save_seconds=-10)


def test_color_palette_selection() -> None:
    """Verify that built-in palettes are fetched and formatted correctly."""
    dark_palette = get_palette(EditorTheme.DARK)
    assert isinstance(dark_palette, ColorPalette)
    assert dark_palette.background == "#0d1117"

    dictionary_palette = dark_palette.to_dict()
    assert dictionary_palette["background"] == "#0d1117"
    assert "keyword" in dictionary_palette


def test_background_worker_concurrency(qtbot: object) -> None:
    """Verify that BackgroundWorker safely offloads tasks with thread signals."""
    cast_qtbot = getattr(qtbot, "waitSignal", None)
    if cast_qtbot is None:
        return

    def slow_calculation(value_a: int, value_b: int) -> int:
        return value_a + value_b

    worker = run_in_thread(slow_calculation, 35, 7)

    results_received: list[int] = []
    worker.signals.result.connect(results_received.append)

    if not results_received:
        with qtbot.waitSignal(worker.signals.finished, timeout=5000):  # type: ignore[attr-defined]
            pass

    assert len(results_received) == 1
    assert results_received[0] == 42


def test_jedi_service_completions(qtbot: object) -> None:
    """Verify that JediService asynchronously queries autocomplete details."""
    cast_qtbot = getattr(qtbot, "waitSignal", None)
    if cast_qtbot is None:
        return

    jedi_service = JediService()

    source_code = "import sys\nsys."
    line_number = 2
    column_number = 4
    file_path = Path("dummy_file.py")

    results_received: list[tuple[int, JediResult]] = []

    def on_jedi_results(token: int, result: JediResult) -> None:
        results_received.append((token, result))

    jedi_service.results.connect(on_jedi_results)

    with qtbot.waitSignal(jedi_service.results, timeout=10000):  # type: ignore[attr-defined]
        token = jedi_service.request_completion(
            source_code, line_number, column_number, file_path
        )

    assert len(results_received) == 1
    received_token, received_result = results_received[0]
    assert received_token == token
    assert received_result.task_type == JediTaskType.COMPLETION
    assert len(received_result.completions) > 0
