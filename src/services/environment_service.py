"""Environment Service for PipViper IDE.

Encapsulates Python interpreter discovery, virtual environment detection,
and active runtime configuration (PRD A3).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import QObject, Signal

from ..environment import (
    EnvironmentDetector,
    PythonEnvironment,
)

_LOGGER = logging.getLogger("src.services.environment")


class EnvironmentService(QObject):
    """Manages active Python runtime and environment discovery."""

    environment_changed = Signal(object)  # Emits PythonEnvironment when switched

    _instance: Optional[EnvironmentService] = None

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._detector = EnvironmentDetector()
        self._active_env: Optional[PythonEnvironment] = None
        self._discovered_envs: list[PythonEnvironment] = []

    @classmethod
    def get_instance(cls) -> EnvironmentService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def detect_environments(self, workspace_root: Optional[Path] = None) -> list[PythonEnvironment]:
        """Discover all available Python environments locally without network access."""
        self._discovered_envs = self._detector.discover_environments(workspace_root)
        if not self._active_env and self._discovered_envs:
            self._active_env = self._discovered_envs[0]
        return self._discovered_envs

    def get_active_environment(self) -> Optional[PythonEnvironment]:
        """Return the currently selected Python runtime environment."""
        return self._active_env

    def get_runtime_python(self) -> str:
        """Return path to the active Python executable."""
        if self._active_env:
            return str(self._active_env.executable)
        import sys
        return sys.executable

    def set_active_environment(self, env: PythonEnvironment) -> None:
        """Switch active Python runtime environment."""
        if self._active_env != env:
            self._active_env = env
            _LOGGER.info("Active environment switched to: %s (%s)", env.name, env.executable)
            self.environment_changed.emit(env)
