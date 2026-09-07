"""Process tree termination and cancellation automated tests (PRD S3, T2).

Verifies:
    * ProcessService.terminate_process_tree terminates running process trees.
    * RunDebugController.stop_execution terminates active execution.
    * Graceful handling of invalid or exited PIDs.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from src.controllers.run_debug_controller import RunDebugController
from src.services.process_service import ProcessService


def test_process_service_terminate_process_tree(tmp_path: Path) -> None:
    """Verify that ProcessService forcefully kills a spawned process tree."""
    service = ProcessService.get_instance()

    # Launch a long-running subprocess
    script = tmp_path / "long_running.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")

    proc = service.spawn_process([sys.executable, str(script)], cwd=tmp_path)
    assert proc.poll() is None  # Still running

    # Terminate process tree
    pid = proc.pid
    service.terminate_process_tree(pid)
    time.sleep(0.5)

    # Process should be terminated
    assert proc.poll() is not None


def test_process_service_terminate_invalid_pid() -> None:
    """Verify that terminate_process_tree gracefully handles non-existent or negative PIDs."""
    service = ProcessService.get_instance()
    # Should not raise
    service.terminate_process_tree(-1)
    service.terminate_process_tree(0)
    service.terminate_process_tree(99999999)


def test_run_debug_controller_stop_execution(qtbot: object, tmp_path: Path) -> None:
    """Verify RunDebugController spawns and stops a running process."""
    cast_qtbot = getattr(qtbot, "waitSignal", None)
    if cast_qtbot is None:
        return

    controller = RunDebugController()

    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")

    started = controller.run_file(script, sys.executable, working_directory=tmp_path)
    assert started is True
    assert controller.is_running is True

    # Give process time to start
    time.sleep(0.3)

    # Stop execution
    controller.stop_execution()
    time.sleep(0.5)

    assert controller.is_running is False
