"""Run and Debug Execution Controller for PipViper IDE.

Orchestrates user code running, process tree cancellation, and debug sessions (PRD A1, S3, S4, S8).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..pip_viper import run_in_thread
from ..services.process_service import ProcessService

_LOGGER = logging.getLogger("src.controllers.run_debug")


class RunDebugController(QObject):
    """Manages spawning, streaming, and terminating script execution and debug sessions."""

    execution_started = Signal(str)      # Emits file path string
    execution_finished = Signal(int)     # Emits return code
    stdout_received = Signal(str)
    stderr_received = Signal(str)
    status_message = Signal(str)

    def __init__(
        self,
        process_service: Optional[ProcessService] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._process_service = process_service or ProcessService.get_instance()
        self._popen_process: Optional[subprocess.Popen[str]] = None
        self._active_file_path: Optional[Path] = None

    @property
    def is_running(self) -> bool:
        return self._popen_process is not None and self._popen_process.poll() is None

    def run_file(
        self,
        file_path: Path,
        python_bin: str,
        working_directory: Optional[Path] = None,
    ) -> bool:
        """Launch a Python file asynchronously and stream output via ProcessService (PRD S8)."""
        if self.is_running:
            self.status_message.emit("A process is already running. Stop it first.")
            return False

        if not file_path.is_file():
            self.status_message.emit(f"Cannot run: File does not exist: {file_path}")
            return False

        self._active_file_path = file_path
        # Enforce working directory in project root or file parent (PRD S4)
        cwd = working_directory or file_path.parent
        cmd = [python_bin, "-u", str(file_path)]

        try:
            self._popen_process = self._process_service.spawn_process(
                cmd=cmd,
                cwd=cwd,
            )
        except Exception as exc:
            _LOGGER.error("Failed to spawn process: %s", exc)
            self.status_message.emit(f"Failed to run: {exc}")
            return False

        self.execution_started.emit(str(file_path))
        self.status_message.emit(f"Running '{file_path.name}'...")

        proc = self._popen_process

        def stream_out() -> None:
            if proc.stdout:
                for line in iter(proc.stdout.readline, ""):
                    if line:
                        self.stdout_received.emit(line)
                proc.stdout.close()
            ret = proc.wait()
            name = self._active_file_path.name if self._active_file_path else "Script"
            self.status_message.emit(f"'{name}' finished with exit code {ret}.")
            self.execution_finished.emit(ret)

        def stream_err() -> None:
            if proc.stderr:
                for line in iter(proc.stderr.readline, ""):
                    if line:
                        self.stderr_received.emit(line)
                proc.stderr.close()

        run_in_thread(stream_out)
        run_in_thread(stream_err)
        return True

    def stop_execution(self) -> None:
        """Forcefully terminate the active process tree (PRD S3)."""
        if self.is_running and self._popen_process:
            pid = self._popen_process.pid
            _LOGGER.info("Stopping execution for process ID %d", pid)
            self._process_service.terminate_process_tree(pid)
            try:
                self._popen_process.kill()
            except Exception:
                pass
            self.status_message.emit("Execution terminated by user.")
