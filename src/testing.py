"""Visual Pytest test runner engine.

Provides background test discovery, hierarchical test item models, and
asynchronous streaming execution via QProcess without blocking the UI thread.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
from PySide6.QtCore import QObject, QProcess, Signal

_LOGGER = logging.getLogger("src.testing")


# -----------------------------------------------------------------------------
# Test Status & Models
# -----------------------------------------------------------------------------


class TestStatus:
    """Status enumeration constants for discovered and executed test items."""

    __test__: bool = False

    PENDING: str = "pending"
    RUNNING: str = "running"
    PASSED: str = "passed"
    FAILED: str = "failed"
    SKIPPED: str = "skipped"
    ERROR: str = "error"


class TestItem(BaseModel):
    """Hierarchical test unit item representing a single Pytest test case."""

    __test__: bool = False

    node_id: str
    file_path: str
    test_name: str
    class_name: Optional[str] = None
    line_number: Optional[int] = None
    status: str = TestStatus.PENDING
    duration_ms: float = 0.0
    traceback: str = ""
    stdout: str = ""

    @property
    def display_name(self) -> str:
        """User-friendly display name for tree visualization."""
        if self.class_name:
            return f"{self.class_name} :: {self.test_name}"
        return self.test_name


class TestSuiteSummary(BaseModel):
    """Execution summary statistics for a completed test run."""

    __test__: bool = False

    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    error: int = 0
    duration_sec: float = 0.0


# -----------------------------------------------------------------------------
# Pytest Engine
# -----------------------------------------------------------------------------


class PytestEngine(QObject):
    """Asynchronous test engine managing discovery and execution via QProcess."""

    discovery_started = Signal()
    discovery_finished = Signal(list)  # list[TestItem]
    discovery_failed = Signal(str)  # error_message

    test_started = Signal(str)  # node_id
    test_finished = Signal(str, str, float, str)  # node_id, status, duration_ms, traceback
    run_finished = Signal(object)  # TestSuiteSummary
    output_received = Signal(str)  # raw line from stdout/stderr

    _TEST_LINE_PATTERN: re.Pattern[str] = re.compile(
        r"^(?P<node_id>[^\s]+::[^\s]+)\s+(?P<status>PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)"
    )
    _SUMMARY_PATTERN: re.Pattern[str] = re.compile(
        r"=+\s+(?:(?P<passed>\d+)\s+passed)?(?:,\s*)?(?:(?P<failed>\d+)\s+failed)?(?:,\s*)?(?:(?P<skipped>\d+)\s+skipped)?(?:,\s*)?(?:(?P<error>\d+)\s+error)?.*in\s+(?P<duration>[\d\.]+)s"
    )

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: Optional[QProcess] = None
        self._test_items_by_id: dict[str, TestItem] = {}
        self._current_running_node: Optional[str] = None
        self._test_start_time: float = 0.0
        self._run_start_time: float = 0.0
        self._raw_output_buffer: list[str] = []
        self._failure_tracebacks: dict[str, list[str]] = {}
        self._active_failure_node: Optional[str] = None

    @staticmethod
    def find_python_executable(project_root: Path | None = None) -> str:
        """Locate the Python executable inside the project venv or active runtime."""
        if project_root:
            if sys.platform == "win32":
                candidates = [
                    project_root / ".venv" / "Scripts" / "python.exe",
                    project_root / "venv" / "Scripts" / "python.exe",
                ]
            else:
                candidates = [
                    project_root / ".venv" / "bin" / "python",
                    project_root / "venv" / "bin" / "python",
                ]
            for candidate in candidates:
                if candidate.is_file():
                    return str(candidate)

        return sys.executable

    def discover_tests(
        self, project_root: Path, python_exe: str | None = None
    ) -> list[TestItem]:
        """Discover all test items in the project workspace using pytest --collect-only."""
        self.discovery_started.emit()
        py_bin = python_exe or self.find_python_executable(project_root)

        cmd = [py_bin, "-m", "pytest", "--collect-only", "-q"]
        try:
            from .services.process_service import ProcessService
            result = ProcessService.get_instance().run_command(
                cmd,
                cwd=str(project_root),
                timeout=30,
            )
            items = self.parse_collection_output(result.stdout, project_root)
            self._test_items_by_id = {item.node_id: item for item in items}
            self.discovery_finished.emit(items)
            return items
        except Exception as exc:
            err_msg = f"Test discovery failed: {exc}"
            _LOGGER.error(err_msg)
            self.discovery_failed.emit(err_msg)
            return []

    @staticmethod
    def parse_collection_output(
        stdout: str, project_root: Path | None = None
    ) -> list[TestItem]:
        """Parse the raw lines of `pytest --collect-only -q` into structured TestItem models."""
        items: list[TestItem] = []
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line or "::" not in line:
                continue

            parts = line.split("::")
            if len(parts) < 2:
                continue

            file_rel = parts[0]
            if len(parts) == 2:
                class_name = None
                test_name = parts[1]
            else:
                class_name = parts[1]
                test_name = parts[2]

            # Try to resolve line number from source file if accessible
            line_no: Optional[int] = None
            if project_root:
                full_path = project_root / file_rel
                if full_path.is_file():
                    line_no = PytestEngine._find_test_line_in_file(full_path, test_name)

            item = TestItem(
                node_id=line,
                file_path=file_rel,
                test_name=test_name,
                class_name=class_name,
                line_number=line_no,
                status=TestStatus.PENDING,
            )
            items.append(item)

        return items

    @staticmethod
    def _find_test_line_in_file(file_path: Path, test_name: str) -> Optional[int]:
        """Find the 1-indexed line number where a test function is declared."""
        clean_name = test_name.split("[")[0]
        pattern = re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(clean_name)}\b")
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f, start=1):
                    if pattern.match(line):
                        return idx
        except OSError:
            pass
        return None

    def is_running(self) -> bool:
        """Return True if a pytest subprocess is actively running."""
        return self._process is not None and self._process.state() == QProcess.ProcessState.Running

    def start_run(
        self,
        project_root: Path,
        python_exe: str | None = None,
        node_ids: list[str] | None = None,
        failed_only: bool = False,
        extra_args: list[str] | None = None,
    ) -> bool:
        """Launch an asynchronous test run via QProcess."""
        if self.is_running():
            _LOGGER.warning("A test run is already in progress.")
            return False

        py_bin = python_exe or self.find_python_executable(project_root)
        args = ["-u", "-m", "pytest", "-v", "--tb=short"]

        if failed_only:
            args.append("--lf")

        if extra_args:
            args.extend(extra_args)

        if node_ids:
            args.extend(node_ids)

        self._raw_output_buffer.clear()
        self._failure_tracebacks.clear()
        self._active_failure_node = None
        self._run_start_time = time.time()
        self._test_start_time = time.time()

        self._process = QProcess(self)
        self._process.setWorkingDirectory(str(project_root))
        self._process.readyReadStandardOutput.connect(self._on_stdout_ready)
        self._process.readyReadStandardError.connect(self._on_stderr_ready)
        self._process.finished.connect(self._on_process_finished)

        self._process.start(py_bin, args)
        return True

    def stop_run(self) -> None:
        """Terminate the running pytest execution process."""
        if self._process is not None and self._process.state() == QProcess.ProcessState.Running:
            self._process.terminate()
            if not self._process.waitForFinished(1000):
                self._process.kill()

    def _on_stdout_ready(self) -> None:
        """Process incoming lines of stdout from the pytest process."""
        if self._process is None:
            return
        data = self._process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        for line in data.splitlines():
            self._handle_output_line(line)

    def _on_stderr_ready(self) -> None:
        """Process incoming lines of stderr from the pytest process."""
        if self._process is None:
            return
        data = self._process.readAllStandardError().data().decode("utf-8", errors="replace")
        for line in data.splitlines():
            self.output_received.emit(line)
            self._raw_output_buffer.append(line)

    def _handle_output_line(self, line: str) -> None:
        """Parse a single output line from pytest, updating test statuses."""
        self.output_received.emit(line)
        self._raw_output_buffer.append(line)

        # 1. Check for individual test execution result: <node_id> <STATUS>
        match = self._TEST_LINE_PATTERN.match(line)
        if match:
            node_id = match.group("node_id")
            raw_status = match.group("status")

            now = time.time()
            duration_ms = max(1.0, (now - self._test_start_time) * 1000.0)
            self._test_start_time = now

            status_map = {
                "PASSED": TestStatus.PASSED,
                "FAILED": TestStatus.FAILED,
                "SKIPPED": TestStatus.SKIPPED,
                "ERROR": TestStatus.ERROR,
                "XFAIL": TestStatus.PASSED,
                "XPASS": TestStatus.PASSED,
            }
            status = status_map.get(raw_status, TestStatus.PASSED)

            if node_id in self._test_items_by_id:
                self._test_items_by_id[node_id].status = status
                self._test_items_by_id[node_id].duration_ms = duration_ms

            self.test_finished.emit(node_id, status, duration_ms, "")
            return

        # 2. Check for failure traceback headers: "___ test_name ___"
        if line.startswith("___") and line.endswith("___"):
            candidate_name = line.replace("_", " ").strip()
            for node_id in self._test_items_by_id:
                if candidate_name in node_id:
                    self._active_failure_node = node_id
                    self._failure_tracebacks[node_id] = [line]
                    break
            return

        # 3. If currently capturing a failure traceback
        if self._active_failure_node:
            if line.startswith("===") or line.startswith("---"):
                self._active_failure_node = None
            else:
                self._failure_tracebacks[self._active_failure_node].append(line)

    def _on_process_finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        """Handle completion of the pytest execution process."""
        total_duration = max(0.01, time.time() - self._run_start_time)

        # Attach captured failure tracebacks to items and re-emit test_finished with tracebacks
        for node_id, tb_lines in self._failure_tracebacks.items():
            tb_str = "\n".join(tb_lines)
            if node_id in self._test_items_by_id:
                item = self._test_items_by_id[node_id]
                item.traceback = tb_str
                self.test_finished.emit(node_id, item.status, item.duration_ms, tb_str)

        # Compute summary
        passed_count = sum(1 for i in self._test_items_by_id.values() if i.status == TestStatus.PASSED)
        failed_count = sum(1 for i in self._test_items_by_id.values() if i.status in (TestStatus.FAILED, TestStatus.ERROR))
        skipped_count = sum(1 for i in self._test_items_by_id.values() if i.status == TestStatus.SKIPPED)
        total_count = len(self._test_items_by_id)

        summary = TestSuiteSummary(
            total=total_count,
            passed=passed_count,
            failed=failed_count,
            skipped=skipped_count,
            error=0,
            duration_sec=round(total_duration, 2),
        )

        self.run_finished.emit(summary)
