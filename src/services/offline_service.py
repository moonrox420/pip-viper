"""Offline Mode service and network gating for PipViper IDE.

This module provides the central authority for PipViper's offline-first mandate:
    * Global offline flag persisted in QSettings (defaulting to True / Offline).
    * Strict network operation assertion and audit logging.
    * Typed OfflineModeError for blocked online paths.
    * Configuration for local wheel packages / offline mirror directories.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QSettings, Signal

_LOGGER: logging.Logger = logging.getLogger("src.services.offline")

_SETTINGS_OFFLINE_KEY = "network/offline_mode"
_SETTINGS_WHEELHOUSE_KEY = "network/local_wheelhouse_dir"


class OfflineModeError(RuntimeError):
    """Raised when an operation requiring external network access is attempted while Offline Mode is active."""

    def __init__(self, message: str = "Operation is blocked because PipViper is operating in Offline Mode.") -> None:
        super().__init__(message)


class OfflineService(QObject):
    """Central authority controlling offline-mode policy and network operation gating."""

    offline_mode_changed = Signal(bool)  # Emitted when offline mode is toggled (True = offline)
    wheelhouse_changed = Signal(str)     # Emitted when local wheelhouse directory is updated

    _instance: Optional[OfflineService] = None

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._settings = QSettings()
        # Non-negotiable PRD requirement: Offline Mode is default on first run
        stored_value = self._settings.value(_SETTINGS_OFFLINE_KEY, True)
        if isinstance(stored_value, str):
            self._is_offline = stored_value.lower() in ("true", "1", "yes")
        else:
            self._is_offline = bool(stored_value)

        stored_wheelhouse = self._settings.value(_SETTINGS_WHEELHOUSE_KEY, "")
        self._local_wheelhouse: Optional[Path] = Path(str(stored_wheelhouse)) if stored_wheelhouse else None

    @classmethod
    def get_instance(cls) -> OfflineService:
        """Access the application-wide OfflineService singleton."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (primarily for isolated test fixtures)."""
        cls._instance = None

    def is_offline(self) -> bool:
        """Return True if the IDE is currently in Offline Mode."""
        return self._is_offline

    def set_offline_mode(self, enabled: bool) -> None:
        """Set the global Offline Mode state and persist it."""
        if self._is_offline != enabled:
            self._is_offline = enabled
            self._settings.setValue(_SETTINGS_OFFLINE_KEY, enabled)
            _LOGGER.info("Offline Mode changed to: %s", "ENABLED (Offline)" if enabled else "DISABLED (Online Opt-in)")
            self.offline_mode_changed.emit(enabled)

    set_offline = set_offline_mode

    def assert_network_allowed(self, feature_name: str) -> None:
        """Verify that network operations are permissible.

        Args:
            feature_name: Human-readable name of the action attempting network access.

        Raises:
            OfflineModeError: If Offline Mode is currently active.
        """
        if self._is_offline:
            error_msg = (
                f"Action '{feature_name}' was blocked: PipViper is running in Offline Mode. "
                f"To use this feature, explicitly switch to Online Mode in the status bar or settings."
            )
            _LOGGER.warning("NETWORK ATTEMPT BLOCKED: %s", error_msg)
            raise OfflineModeError(error_msg)
        _LOGGER.info("Permitting opt-in network action: %s", feature_name)

    def get_local_wheelhouse_dir(self) -> Optional[Path]:
        """Return the user-configured local wheel/package cache directory, if any."""
        if self._local_wheelhouse and self._local_wheelhouse.is_dir():
            return self._local_wheelhouse
        return None

    def set_local_wheelhouse_dir(self, path: Optional[Path]) -> None:
        """Configure a local directory of wheel packages for offline installs."""
        self._local_wheelhouse = path
        path_str = str(path) if path else ""
        self._settings.setValue(_SETTINGS_WHEELHOUSE_KEY, path_str)
        _LOGGER.info("Local wheelhouse directory configured: %s", path_str or "(None)")
        self.wheelhouse_changed.emit(path_str)


def assert_network_allowed(feature_name: str) -> None:
    """Convenience module function that checks network permission via the OfflineService singleton."""
    OfflineService.get_instance().assert_network_allowed(feature_name)
