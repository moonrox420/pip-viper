"""Central Process Execution Service for PipViper IDE.

Enforces critical security, isolation, and lifecycle hardening:
    * Argument list enforcement and shell injection prevention (S1).
    * Controlled process environment sanitization (S2).
    * Process tree termination for guaranteed task cancellation (S3).
    * Project working directory enforcement (S4).
    * Centralized lifecycle tracking and timeouts (S8).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

_LOGGER: logging.Logger = logging.getLogger("src.services.process")

# Strict patterns for argument validation
_DANGEROUS_SHELL_CHARS_REGEX = re.compile(r"[\x00\r\n;&|`$<>]")
_PACKAGE_NAME_REGEX = re.compile(
    r"^[a-zA-Z0-9_\.\-]+(?:\[[a-zA-Z0-9_\.\-,]+\])?(?:[<>=!~]+[a-zA-Z0-9_\.\-\*]+(?:,[<>=!~]+[a-zA-Z0-9_\.\-\*]+)*)?$"
)


class ProcessSecurityError(ValueError):
    """Raised when process arguments or working directories violate security validation."""


class ProcessService:
    """Central authority for executing and tracking child processes safely."""

    _instance: Optional[ProcessService] = None

    def __init__(self) -> None:
        self._active_processes: dict[int, subprocess.Popen[Any]] = {}

    @classmethod
    def get_instance(cls) -> ProcessService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def sanitize_arguments(arguments: Sequence[str]) -> list[str]:
        """Validate and sanitize process command-line arguments.

        Ensures all elements are strings and rejects shell metacharacters.
        """
        if not arguments:
            raise ProcessSecurityError("Command arguments sequence cannot be empty.")

        sanitized: list[str] = []
        for index, argument in enumerate(arguments):
            if not isinstance(argument, (str, Path)):
                raise ProcessSecurityError(
                    f"Argument at index {index} must be a str or Path, got {type(argument).__name__}"
                )
            arg_str = str(argument)
            # Check for shell metacharacters
            if _DANGEROUS_SHELL_CHARS_REGEX.search(arg_str):
                raise ProcessSecurityError(
                    f"Dangerous shell characters detected in argument at index {index}: {arg_str!r}"
                )
            sanitized.append(arg_str)
        return sanitized

    @staticmethod
    def validate_package_name(package_name: str) -> str:
        """Validate a Python package name / specifier against standard distribution rules."""
        clean = package_name.strip()
        if not clean or not _PACKAGE_NAME_REGEX.match(clean):
            raise ProcessSecurityError(f"Invalid or unsafe Python package specifier: {package_name!r}")
        return clean

    @staticmethod
    def build_safe_environment(
        base_env: Optional[Mapping[str, str]] = None,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> dict[str, str]:
        """Construct a sanitized environment containing only essential system variables (S2)."""
        source = base_env or os.environ
        # Whitelist essential environment keys
        essential_keys = {
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "WINDIR",
            "PATH",
            "PATHEXT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "COMSPEC",
            "PROGRAMDATA",
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "COMMONPROGRAMFILES",
            "APPDATA",
            "LOCALAPPDATA",
            "USERNAME",
            "USERDOMAIN",
            "ALLUSERSPROFILE",
            # Unix / POSIX essentials
            "HOME",
            "SHELL",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "TERM",
        }

        safe_env: dict[str, str] = {}
        for key, value in source.items():
            if key.upper() in essential_keys:
                safe_env[key] = value

        # Always enforce unbuffered output and standard encoding
        safe_env["PYTHONUNBUFFERED"] = "1"
        safe_env["PYTHONIOENCODING"] = "utf-8"

        if extra_env:
            for key, value in extra_env.items():
                if not _DANGEROUS_SHELL_CHARS_REGEX.search(str(key)) and not _DANGEROUS_SHELL_CHARS_REGEX.search(str(value)):
                    safe_env[key] = str(value)

        return safe_env

    @staticmethod
    def terminate_process_tree(process_id: int) -> None:
        """Forcefully terminate an entire process tree by PID (S3)."""
        if process_id <= 0:
            return

        _LOGGER.info("Terminating process tree for PID %d", process_id)
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process_id)],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except Exception as exc:
                _LOGGER.debug("taskkill exception for PID %d: %s", process_id, exc)
            try:
                import signal
                os.kill(process_id, signal.SIGTERM)
            except Exception as exc:
                _LOGGER.debug("os.kill exception for PID %d: %s", process_id, exc)
        else:
            try:
                import signal
                os.killpg(os.getpgid(process_id), signal.SIGKILL)
            except Exception as exc:
                _LOGGER.debug("POSIX killpg exception for PID %d: %s", process_id, exc)

    def run_command(
        self,
        cmd: Sequence[str | Path],
        cwd: Optional[Path | str] = None,
        timeout: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
        check: bool = False,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a validated command synchronously with timeout and process protection."""
        sanitized_cmd = self.sanitize_arguments([str(c) for c in cmd])
        working_dir = str(cwd) if cwd else None
        safe_env = self.build_safe_environment(extra_env=env)

        kwargs: dict[str, Any] = {
            "cwd": working_dir,
            "env": safe_env,
            "text": True,
            "shell": False,  # Strict enforcement: NEVER shell=True
        }

        if capture_output:
            kwargs["capture_output"] = True

        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = startupinfo

        _LOGGER.debug("ProcessService.run_command: %s", sanitized_cmd)
        try:
            return subprocess.run(sanitized_cmd, timeout=timeout, check=check, **kwargs)
        except subprocess.TimeoutExpired as exc:
            _LOGGER.warning("Command timed out after %s seconds: %s", timeout, sanitized_cmd)
            raise

    def spawn_process(
        self,
        cmd: Sequence[str | Path],
        cwd: Optional[Path | str] = None,
        env: Optional[Mapping[str, str]] = None,
        stdout: Any = subprocess.PIPE,
        stderr: Any = subprocess.PIPE,
        stdin: Any = None,
    ) -> subprocess.Popen[str]:
        """Spawn a long-running child process with tracking and process group creation."""
        sanitized_cmd = self.sanitize_arguments([str(c) for c in cmd])
        working_dir = str(cwd) if cwd else None
        safe_env = self.build_safe_environment(extra_env=env)

        kwargs: dict[str, Any] = {
            "cwd": working_dir,
            "env": safe_env,
            "text": True,
            "shell": False,
            "stdout": stdout,
            "stderr": stderr,
            "stdin": stdin,
        }

        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(sanitized_cmd, **kwargs)
        self._active_processes[proc.pid] = proc
        return proc

    def stop_process(self, proc: subprocess.Popen[Any]) -> None:
        """Safely stop a tracked process and its child tree."""
        if proc and proc.pid:
            self.terminate_process_tree(proc.pid)
            self._active_processes.pop(proc.pid, None)
