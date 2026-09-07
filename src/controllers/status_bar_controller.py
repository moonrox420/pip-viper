"""Status Bar Controller for PipViper IDE.

Orchestrates status bar indicators:
    * Permanent Offline Mode badge (PRD U1).
    * Real-time Memory Meter telemetry (Milestone 8).
    * Active Python interpreter selector trigger.
    * Cursor position coordinates.
    * Git branch indicator.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QLabel, QMenu, QStatusBar, QWidget

from ..environment import PythonEnvironment
from ..widgets import MemoryMeterWidget, OfflineModeBadge

_LOGGER = logging.getLogger("src.controllers.status_bar")


class StatusBarController(QObject):
    """Encapsulates creation and updates for all QStatusBar child widgets."""

    switch_environment_requested = Signal()
    open_memory_studio_requested = Signal()

    def __init__(
        self,
        status_bar: QStatusBar,
        palette: Any = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._status_bar = status_bar
        self._palette = palette

        # 1. Main Status Message Label
        self.status_label = QLabel("Ready", status_bar)
        self._status_bar.addWidget(self.status_label, 1)

        # 2. Git Branch Badge
        self.branch_label = QLabel("", status_bar)
        self._status_bar.addPermanentWidget(self.branch_label)

        # 3. Cursor Position Label (Ln X, Col Y)
        self.cursor_label = QLabel("Ln 1, Col 1", status_bar)
        self._status_bar.addPermanentWidget(self.cursor_label)

        # 4. Active Python Interpreter Label
        self.interpreter_label = QLabel("Python", status_bar)
        self.interpreter_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.interpreter_label.mousePressEvent = self._on_interpreter_clicked
        self._status_bar.addPermanentWidget(self.interpreter_label)

        # 5. Offline Mode Badge (PRD U1)
        self.offline_badge = OfflineModeBadge(palette=palette, parent=status_bar)
        self._status_bar.addPermanentWidget(self.offline_badge)

        # 6. Memory Meter Widget
        self.memory_meter = MemoryMeterWidget(palette=palette, parent=status_bar)
        self.memory_meter.open_memory_profiler_requested.connect(
            self.open_memory_studio_requested.emit
        )
        self._status_bar.addPermanentWidget(self.memory_meter)

        self.apply_palette(palette)

    def apply_palette(self, palette: Any) -> None:
        self._palette = palette
        if not palette:
            return

        text_color = getattr(palette, "muted", "#888888")
        accent_color = getattr(palette, "accent", "#58a6ff")
        css = f"color: {text_color}; font-size: 11px; padding: 0 6px;"
        self.status_label.setStyleSheet(css)
        self.branch_label.setStyleSheet(f"color: {accent_color}; font-size: 11px; padding: 0 6px;")
        self.cursor_label.setStyleSheet(css)
        self.interpreter_label.setStyleSheet(f"color: {text_color}; font-size: 11px; padding: 0 6px;")

        self.offline_badge.set_palette(palette)
        self.memory_meter.set_palette(palette)

    def set_message(self, message: str, timeout_ms: int = 0) -> None:
        self.status_label.setText(message)
        if timeout_ms > 0:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(timeout_ms, lambda: self.status_label.setText("Ready"))

    def update_cursor_position(self, line: int, column: int) -> None:
        self.cursor_label.setText(f"Ln {line}, Col {column}")

    def update_interpreter(self, env: Optional[PythonEnvironment]) -> None:
        if env:
            ver = f"Python {env.version}" if env.version else "Python"
            self.interpreter_label.setText(f"🐍 {ver} ({env.name})")
            self.interpreter_label.setToolTip(f"Active Interpreter: {env.executable}\nClick to switch.")
        else:
            self.interpreter_label.setText("🐍 Python")
            self.interpreter_label.setToolTip("Click to select Python environment.")

    def update_git_branch(self, branch_name: str) -> None:
        if branch_name:
            self.branch_label.setText(f"🌿 {branch_name}")
            self.branch_label.setToolTip(f"Git Current Branch: {branch_name}")
        else:
            self.branch_label.setText("")
            self.branch_label.setToolTip("")

    def _on_interpreter_clicked(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.switch_environment_requested.emit()
