"""Layout and Dock Panels Controller for PipViper IDE.

Orchestrates window geometry, splitters, bottom tabs, and view actions (PRD A1).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import QByteArray, QObject, QSettings
from PySide6.QtWidgets import QMainWindow, QSplitter, QTabWidget, QWidget

_LOGGER = logging.getLogger("src.controllers.layout")

_LAYOUT_SCHEMA_VERSION = 3


class LayoutController(QObject):
    """Manages workspace layout panes, dock visibility, and geometry persistence."""

    def __init__(
        self,
        main_window: QMainWindow,
        main_splitter: QSplitter,
        content_splitter: QSplitter,
        sidebar_splitter: QSplitter,
        bottom_tabs: QTabWidget,
        ai_panel: Optional[QWidget] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._window = main_window
        self._main_splitter = main_splitter
        self._content_splitter = content_splitter
        self._sidebar_splitter = sidebar_splitter
        self._bottom_tabs = bottom_tabs
        self._ai_panel = ai_panel
        self._settings = QSettings("PipViper", "PipViperIDE")
        self._ai_panel_last_width: int = 340
        self._sidebar_last_width: int = 300

    def restore_layout(self) -> None:
        """Restore window geometry and splitter sizes saved from a prior session."""
        saved_geometry = self._settings.value("window/geometry")
        if saved_geometry is not None:
            self._window.restoreGeometry(saved_geometry)

        saved_schema_version = self._settings.value("layout/schemaVersion", 0, type=int)
        if saved_schema_version != _LAYOUT_SCHEMA_VERSION:
            return

        for settings_key, splitter in (
            ("layout/sidebarSplitter", self._sidebar_splitter),
            ("layout/contentSplitter", self._content_splitter),
            ("layout/mainSplitter", self._main_splitter),
        ):
            saved_state = self._settings.value(settings_key)
            if saved_state is not None:
                splitter.restoreState(saved_state)

    def save_layout(self) -> None:
        """Persist current geometry and splitter configurations."""
        self._settings.setValue("window/geometry", self._window.saveGeometry())
        self._settings.setValue("layout/schemaVersion", _LAYOUT_SCHEMA_VERSION)
        self._settings.setValue("layout/sidebarSplitter", self._sidebar_splitter.saveState())
        self._settings.setValue("layout/contentSplitter", self._content_splitter.saveState())
        self._settings.setValue("layout/mainSplitter", self._main_splitter.saveState())

    def toggle_bottom_panel(self) -> None:
        """Toggle bottom tabs visibility."""
        is_visible = self._bottom_tabs.isVisible()
        self._bottom_tabs.setVisible(not is_visible)

    def show_panel_tab(self, widget: QWidget) -> None:
        """Ensure bottom tabs are visible and switch to target panel."""
        self._bottom_tabs.setVisible(True)
        self._bottom_tabs.setCurrentWidget(widget)

    def toggle_sidebar(self) -> None:
        """Toggle left workspace sidebar."""
        sizes = self._content_splitter.sizes()
        if not sizes:
            return
        if sizes[0] > 0:
            self._sidebar_last_width = sizes[0]
            sizes[0] = 0
        else:
            sizes[0] = self._sidebar_last_width or 300
        self._content_splitter.setSizes(sizes)

    def toggle_ai_panel(self) -> None:
        """Toggle right AI assistant panel."""
        if not self._ai_panel:
            return
        sizes = self._content_splitter.sizes()
        if len(sizes) < 3:
            return
        if sizes[2] > 0:
            self._ai_panel_last_width = sizes[2]
            sizes[2] = 0
        else:
            sizes[2] = self._ai_panel_last_width or 340
        self._content_splitter.setSizes(sizes)
