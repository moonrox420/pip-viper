"""AI Assistant Controller for PipViper IDE.

Orchestrates local AI interactions, mandatory diff review dialogs, and sidecar health (PRD A1, U2, U4).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from ..pip_viper import ColorPalette, ProcessError, run_in_thread
from ..services.ai_service import AIService

_LOGGER = logging.getLogger("src.controllers.ai")


def extract_code_from_markdown(text: str) -> str:
    """Extract Python source from markdown code blocks."""
    pattern = r"```[ \t]*python\s*\n(.*?)\n\s*```"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()

    generic_pattern = r"```\s*\n(.*?)\n\s*```"
    match = re.search(generic_pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
    if cleaned.endswith("```"):
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return cleaned.strip()


class AIController(QObject):
    """Coordinates local AI assistant queries, diff review, and editor mutations."""

    code_mutation_accepted = Signal(str)  # Emits replacement code accepted by user
    status_message = Signal(str)

    def __init__(
        self,
        ai_service: Optional[AIService] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._service = ai_service or AIService.get_instance()

    @property
    def service(self) -> AIService:
        return self._service

    def request_refactor(
        self,
        original_code: str,
        user_instructions: str,
        palette: ColorPalette,
        parent_widget: Optional[QWidget] = None,
        diff_dialog_factory: Optional[Callable[[str, str, ColorPalette, Optional[QWidget]], QDialog]] = None,
    ) -> None:
        """Query local AI sidecar for code adjustments and present mandatory diff review."""
        system_prompt = (
            "You are an expert Python engineer. Refactor the provided code cleanly according "
            "to user instructions. Return ONLY valid, complete Python code wrapped in a single ```python block."
        )
        user_prompt = f"Instructions: {user_instructions}\n\nCode to refactor:\n```python\n{original_code}\n```"

        self.status_message.emit("Querying local AI sidecar...")

        def worker() -> str:
            return self._service.query(system_prompt, user_prompt)

        def on_finished(result: str) -> None:
            extracted = extract_code_from_markdown(result)
            if not extracted:
                extracted = result.strip()

            self.status_message.emit("AI suggestion generated. Review diffs to accept.")

            # Mandatory Diff Review (PRD U4)
            if diff_dialog_factory:
                dialog = diff_dialog_factory(original_code, extracted, palette, parent_widget)
                if dialog.exec() == QDialog.DialogCode.Accepted:
                    # Fetch modified code if method exists
                    new_code = getattr(dialog, "get_modified_code", lambda: extracted)()
                    self.code_mutation_accepted.emit(new_code)
                    self.status_message.emit("AI refactoring applied to document.")
                else:
                    self.status_message.emit("AI suggestion discarded.")

        def on_error(err_msg: str) -> None:
            _LOGGER.warning("Local AI query failed: %s", err_msg)
            self.status_message.emit(f"AI Assistant Error: {err_msg}")
            if parent_widget:
                QMessageBox.warning(parent_widget, "AI Assistant Error", str(err_msg))

        bg_worker = run_in_thread(worker)
        bg_worker.signals.result.connect(on_finished)
        bg_worker.signals.error.connect(on_error)
