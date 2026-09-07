"""Editor Controller for PipViper IDE.

Orchestrates document life-cycle:
    * File open, save, save-as, close (PRD A1).
    * Editor tab management and split editor synchronization.
    * Gutter diff update triggers.
    * Line/column navigation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from ..editor import CodeEditor, EditorTabs
from ..pip_viper import ColorPalette, FileReadError, FileWriteError

_LOGGER = logging.getLogger("src.controllers.editor")


class EditorController(QObject):
    """Manages workspace editor buffers, tabs, and file I/O operations."""

    file_opened = Signal(Path)
    file_saved = Signal(Path)
    current_editor_changed = Signal(object)  # Emits CodeEditor
    status_message = Signal(str)

    def __init__(
        self,
        editor_tabs: EditorTabs,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._tabs = editor_tabs
        self._tabs.editor_changed.connect(self.current_editor_changed.emit)

    @property
    def tabs(self) -> EditorTabs:
        return self._tabs

    def get_current_editor(self) -> Optional[CodeEditor]:
        return self._tabs.current_editor()

    def get_current_file_path(self) -> Optional[Path]:
        editor = self.get_current_editor()
        return editor.file_path() if editor else None

    def open_file(self, file_path: Path) -> Optional[CodeEditor]:
        """Open a file into the editor tabs and emit signal."""
        path = file_path.resolve()
        if not path.is_file():
            self.status_message.emit(f"File not found: {path}")
            return None

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            _LOGGER.exception("Failed to read %s: %s", path, exc)
            self.status_message.emit(f"Failed to read {path.name}: {exc}")
            return None

        editor = self._tabs.add_editor(path, content)
        editor.set_file_path(path)
        self.file_opened.emit(path)
        self.status_message.emit(f"Opened: {path.name}")
        return editor

    def new_file(self) -> CodeEditor:
        """Create a new untitled buffer tab."""
        path = Path(f"untitled_{self._tabs.editor_count() + 1}.py")
        editor = self._tabs.add_editor(path, "")
        editor.set_file_path(path)
        self.status_message.emit("Created new untitled document.")
        return editor

    def save_current_file(self, parent_widget: Optional[QWidget] = None) -> bool:
        """Save the active editor buffer to disk."""
        editor = self.get_current_editor()
        if not editor:
            return False

        fp = editor.file_path()
        if fp and not fp.name.startswith("untitled_"):
            try:
                content = editor.toPlainText()
                fp.write_text(content, encoding="utf-8")
                editor.mark_saved()
                self.file_saved.emit(fp)
                self.status_message.emit(f"Saved: {fp.name}")
                return True
            except Exception as exc:
                msg = f"Failed saving {fp.name}: {exc}"
                _LOGGER.error(msg)
                if parent_widget:
                    QMessageBox.critical(parent_widget, "Save Error", msg)
                return False
        else:
            return self.save_current_file_as(parent_widget)

    def save_current_file_as(self, parent_widget: Optional[QWidget] = None) -> bool:
        """Prompt for a file location and save the buffer."""
        editor = self.get_current_editor()
        if not editor:
            return False

        selected_path, _ = QFileDialog.getSaveFileName(
            parent_widget, "Save File As", "", "Python Files (*.py);;All Files (*)"
        )
        if not selected_path:
            return False

        target_file = Path(selected_path).resolve()
        try:
            content = editor.toPlainText()
            target_file.write_text(content, encoding="utf-8")
            editor.set_file_path(target_file)
            editor.mark_saved()
            self.file_saved.emit(target_file)
            self.status_message.emit(f"Saved as: {target_file.name}")
            return True
        except Exception as exc:
            msg = f"Failed saving {target_file.name}: {exc}"
            _LOGGER.error(msg)
            if parent_widget:
                QMessageBox.critical(parent_widget, "Save Error", msg)
            return False

    def navigate_to_line(self, file_path: Path | str, line_number: int, column_number: int = 1) -> None:
        """Focus or open target file and jump to line."""
        target = Path(file_path).resolve()
        editor = self.open_file(target) if target.is_file() else None
        if editor:
            editor.jump_to_line(line_number, column_number)

