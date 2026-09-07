"""Environment Controller for PipViper IDE.

Manages active Python interpreter selection and dialog workflows (PRD A1).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from ..environment import (
    EnvironmentPickerDialog,
    PythonEnvironment,
)
from ..services.environment_service import EnvironmentService

_LOGGER = logging.getLogger("src.controllers.environment")


class EnvironmentController(QObject):
    """Coordinates Python environment discovery, picking, and state updates."""

    environment_selected = Signal(object)  # Emits PythonEnvironment

    def __init__(
        self,
        service: Optional[EnvironmentService] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._service = service or EnvironmentService.get_instance()
        self._service.environment_changed.connect(self.environment_selected.emit)

    @property
    def service(self) -> EnvironmentService:
        return self._service

    def get_runtime_python(self) -> str:
        return self._service.get_runtime_python()

    def get_active_environment(self) -> Optional[PythonEnvironment]:
        return self._service.get_active_environment()

    def show_environment_picker(
        self,
        parent_widget: Optional[QWidget] = None,
        project_root: Optional[Path] = None,
        palette: Any = None,
    ) -> Optional[PythonEnvironment]:
        """Display modal environment picker and switch active runtime if selected."""
        envs = self._service.detect_environments(project_root)
        active = self._service.get_active_environment()
        dialog = EnvironmentPickerDialog(
            environments=envs,
            active_environment=active,
            palette=palette,
            parent=parent_widget,
        )
        if dialog.exec() and dialog.selected_environment:
            selected = dialog.selected_environment
            self._service.set_active_environment(selected)
            return selected
        return None
