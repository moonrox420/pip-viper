"""Subpanel module extracted from src.panels."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, ClassVar, Optional, Sequence, cast

from pydantic import BaseModel, ConfigDict, Field
from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QObject,
    QPoint,
    QProcess,
    QProcessEnvironment,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..vcs import (
    GitBranch,
    GitCommit,
    GitFileStatus,
    GitService,
    GitStatusEntry,
)
from ..code_tools import CodeIssue
from ..internals import (
    AstInspectionResult,
    AstNodeInfo,
    DisassemblyInstruction,
    DisassemblyResult,
    MemoryHotspot,
    ProfileRecord,
    ProfileResult,
    ScopeInfo,
    ScopeSymbol,
    SymtableResult,
)
from ..testing import TestItem, TestStatus, TestSuiteSummary
from ..dependencies import (
    DependencyGraph,
    DependencyGraphView,
    DependencyNode,
    DependencyScanner,
)
from ..profiler_visualizer import FlameGraphWidget, CallHierarchyView
from ..memory_tracker import MemoryTrackerWidget
from .. import (
    ColorPalette,
    FileOperationError,
    LintIssue,
    LintSeverity,
    PipPackage,
    ProcessTimeoutError,
    run_in_thread,
)
from .base import (
    _LOGGER,
    _SEVERITY_COLOR_KEYS,
    _severity_color,
    _section_label,
    _qcolor,
)

# -----------------------------------------------------------------------------
# REPL Panel with Live Variable Explorer
# -----------------------------------------------------------------------------


class ReplVariable(BaseModel):
    """A variable resident within the interactive REPL namespace."""

    model_config = ConfigDict(frozen=True)

    name: str
    type_name: str
    size_repr: str
    memory_bytes: int
    value_preview: str


class ReplPanel(QWidget):
    """An interactive subshell session with command buffering, history navigation,
    and a live side-by-side Variable Explorer powered by src.repl_harness.
    """

    command_submitted = Signal(str)
    variable_activated = Signal(str)  # Emits variable name on double-click

    START_MARKER: ClassVar[str] = "__PIPVIPER_VARS_START__"
    END_MARKER: ClassVar[str] = "__PIPVIPER_VARS_END__"

    def __init__(
        self,
        palette: ColorPalette,
        python_executable: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette: ColorPalette = palette
        self._python: str = python_executable
        self._project_root: Path | None = None
        self._process: Optional[QProcess] = None
        self._history: list[str] = []
        self._history_index: int = -1
        self._stdout_buffer: str = ""
        self._all_variables: list[ReplVariable] = []

        # --- Left Pane: Interactive Python Shell ---
        self._output_view: QPlainTextEdit = QPlainTextEdit(self)
        self._output_view.setReadOnly(True)
        self._output_view.setFont(QFont("Consolas", 11))
        self._output_view.setMaximumBlockCount(5000)

        self._input_field: QLineEdit = QLineEdit(self)
        self._input_field.setFont(QFont("Consolas", 11))
        self._input_field.setPlaceholderText("Enter Python statement or expression...")
        self._input_field.returnPressed.connect(self._on_submit)
        self._input_field.installEventFilter(self)

        send_button = QPushButton("⏎ Send", self)
        send_button.setProperty("role", "primary")
        send_button.clicked.connect(self._on_submit)

        reset_button = QPushButton("🔄 Reset", self)
        reset_button.setToolTip("Restart the interactive subshell session.")
        reset_button.clicked.connect(self.reset_process)

        interrupt_button = QPushButton("⏹ Interrupt", self)
        interrupt_button.setToolTip("Send interrupt signal to the running REPL.")
        interrupt_button.clicked.connect(self.interrupt_process)

        input_row = QHBoxLayout()
        input_row.addWidget(QLabel(">>>", self))
        input_row.addWidget(self._input_field, 1)
        input_row.addWidget(send_button)
        input_row.addWidget(interrupt_button)
        input_row.addWidget(reset_button)

        shell_box = QGroupBox("🐚 Interactive Python Shell", self)
        shell_layout = QVBoxLayout(shell_box)
        shell_layout.addWidget(self._output_view)
        shell_layout.addLayout(input_row)

        # --- Right Pane: Live Variable Explorer ---
        self._var_filter_input = QLineEdit(self)
        self._var_filter_input.setPlaceholderText("Filter variables by name...")
        self._var_filter_input.textChanged.connect(self._apply_variable_filter)

        refresh_vars_btn = QPushButton("🔄 Refresh", self)
        refresh_vars_btn.setToolTip("Query current namespace variables from the REPL.")
        refresh_vars_btn.clicked.connect(self.request_variable_refresh)

        var_toolbar = QHBoxLayout()
        var_toolbar.addWidget(self._var_filter_input, 1)
        var_toolbar.addWidget(refresh_vars_btn)

        self._variables_table = QTableWidget(0, 5, self)
        self._variables_table.setHorizontalHeaderLabels(
            ["Name", "Type", "Size / Shape", "Memory", "Value Preview"]
        )
        self._variables_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self._variables_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._variables_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._variables_table.setColumnWidth(0, 100)
        self._variables_table.setColumnWidth(1, 90)
        self._variables_table.setColumnWidth(2, 90)
        self._variables_table.setColumnWidth(3, 75)
        self._variables_table.itemDoubleClicked.connect(self._on_variable_double_clicked)

        var_box = QGroupBox("📊 Live Variable Explorer", self)
        var_layout = QVBoxLayout(var_box)
        var_layout.addLayout(var_toolbar)
        var_layout.addWidget(self._variables_table)

        # Assemble Splitter
        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.addWidget(shell_box)
        self._splitter.addWidget(var_box)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 2)
        self._splitter.setSizes([600, 400])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._splitter)

        self._apply_colors()
        self.reset_process()

    def set_palette(self, palette: ColorPalette) -> None:
        """Update stream and table colors dynamically on theme changes."""
        self._palette = palette
        self._apply_colors()

    def _apply_colors(self) -> None:
        self._output_view.setStyleSheet(
            f"background-color: {self._palette.background}; color: {self._palette.text};"
        )
        self._variables_table.setStyleSheet(
            f"background-color: {self._palette.background}; color: {self._palette.text};"
        )

    def set_project_root(self, root: Path | None) -> None:
        """Update workspace root context and restart session if changed."""
        self._project_root = root

    def eventFilter(self, watched_object: QObject, event: QEvent) -> bool:
        """Intercept arrow keys inside input field to scroll previous commands history."""
        if watched_object is self._input_field and event.type() == QEvent.Type.KeyPress:
            key_event = cast(QKeyEvent, event)
            key = key_event.key()
            if key == Qt.Key.Key_Up:
                if self._history:
                    if self._history_index > 0:
                        self._history_index -= 1
                    elif self._history_index == -1:
                        self._history_index = len(self._history) - 1
                    self._input_field.setText(self._history[self._history_index])
                return True
            elif key == Qt.Key.Key_Down:
                if self._history:
                    if 0 <= self._history_index < len(self._history) - 1:
                        self._history_index += 1
                        self._input_field.setText(self._history[self._history_index])
                    else:
                        self._history_index = -1
                        self._input_field.clear()
                return True
        return super().eventFilter(watched_object, event)

    def reset_process(self) -> None:
        """Kill the current Python interactive process and restart with the harness."""
        if self._process is not None:
            try:
                self._process.kill()
                self._process.waitForFinished(1000)
            except Exception as exception:
                _LOGGER.debug("Process cleanup warning: %s", exception)

        self._output_view.clear()
        self._variables_table.setRowCount(0)
        self._all_variables.clear()
        self._stdout_buffer = ""

        self._process = QProcess(self)
        self._process.setProgram(self._python)

        # Locate src/repl_harness.py directly
        harness_path = Path(__file__).resolve().parent / "repl_harness.py"
        self._process.setArguments(["-u", str(harness_path)])

        if self._project_root:
            self._process.setWorkingDirectory(str(self._project_root))

        # Ensure PYTHONPATH includes workspace root so local modules import smoothly
        env = QProcessEnvironment.systemEnvironment()
        workspace_dir = str(Path(__file__).resolve().parent.parent)
        current_pythonpath = env.value("PYTHONPATH")
        env.insert(
            "PYTHONPATH",
            f"{workspace_dir};{current_pythonpath}" if current_pythonpath else workspace_dir,
        )
        self._process.setProcessEnvironment(env)

        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._on_process_output)
        self._process.errorOccurred.connect(self._on_process_error)
        self._process.start()

    def set_python_executable(self, python_executable: str) -> None:
        """Update the target interpreter path for the subshell."""
        self._python = python_executable

    def interrupt_process(self) -> None:
        """Attempt soft interrupt or notify user."""
        if self._process is not None and self._process.state() == QProcess.ProcessState.Running:
            self._process.write(b"\x03")  # Send Ctrl+C byte

    def request_variable_refresh(self) -> None:
        """Request active namespace serialization from the REPL harness."""
        if self._process is not None and self._process.state() == QProcess.ProcessState.Running:
            self._process.write(b"__pipviper_inspect__()\n")

    def execute_code(self, code_str: str) -> None:
        """Dispatch a single or multi-line code string to the REPL."""
        if self._process is None or not code_str.strip():
            return

        lines = code_str.strip("\r\n").splitlines()
        if len(lines) > 1:
            for idx, line in enumerate(lines):
                prompt = ">>> " if idx == 0 else "... "
                self._output_view.appendPlainText(f"{prompt}{line}")
            payload = f"__PIPVIPER_BLOCK_START__\n{code_str}\n__PIPVIPER_BLOCK_END__\n"
            self._process.write(payload.encode("utf-8"))
        else:
            single_line = lines[0]
            self._history.append(single_line)
            self._history_index = -1
            self._output_view.appendPlainText(f">>> {single_line}")
            self._process.write(f"{single_line}\n".encode("utf-8"))

    def cleanup(self) -> None:
        """Terminate and clean up active REPL subprocess."""
        if self._process is not None:
            try:
                self._process.kill()
                self._process.waitForFinished(1000)
            except Exception:
                pass
            self._process = None

    @Slot()
    def _on_process_output(self) -> None:
        if self._process is None:
            return
        output_data = bytes(self._process.readAllStandardOutput().data()).decode(
            "utf-8", errors="replace"
        )
        self._stdout_buffer += output_data

        # Parse delimiter protocol for variables JSON
        while self.START_MARKER in self._stdout_buffer and self.END_MARKER in self._stdout_buffer:
            start_idx = self._stdout_buffer.index(self.START_MARKER)
            end_idx = self._stdout_buffer.index(self.END_MARKER)

            if start_idx < end_idx:
                pre_text = self._stdout_buffer[:start_idx].strip("\r\n")
                if pre_text:
                    self._output_view.appendPlainText(pre_text)

                json_str = self._stdout_buffer[
                    start_idx + len(self.START_MARKER) : end_idx
                ].strip()
                self._parse_variables_json(json_str)

                self._stdout_buffer = self._stdout_buffer[
                    end_idx + len(self.END_MARKER) :
                ]
            else:
                self._stdout_buffer = self._stdout_buffer[
                    end_idx + len(self.END_MARKER) :
                ]

        if self.START_MARKER not in self._stdout_buffer and self._stdout_buffer:
            self._output_view.appendPlainText(self._stdout_buffer.rstrip("\r\n"))
            self._stdout_buffer = ""

    def _parse_variables_json(self, json_str: str) -> None:
        try:
            raw_vars = json.loads(json_str)
            self._all_variables = [
                ReplVariable(
                    name=v["name"],
                    type_name=v.get("type_name", "object"),
                    size_repr=v.get("size_repr", "-"),
                    memory_bytes=v.get("memory_bytes", 0),
                    value_preview=v.get("value_preview", ""),
                )
                for v in raw_vars
            ]
            self._apply_variable_filter(self._var_filter_input.text())
        except Exception as exc:
            _LOGGER.debug("Failed parsing REPL variables JSON: %s", exc)

    def _apply_variable_filter(self, filter_text: str) -> None:
        stripped = filter_text.strip().lower()
        if not stripped:
            matched = self._all_variables
        else:
            matched = [v for v in self._all_variables if stripped in v.name.lower()]

        self._variables_table.setRowCount(len(matched))
        for row_idx, var in enumerate(matched):
            name_item = QTableWidgetItem(var.name)
            name_item.setData(Qt.ItemDataRole.UserRole, var.name)
            type_item = QTableWidgetItem(var.type_name)
            size_item = QTableWidgetItem(var.size_repr)
            mem_item = QTableWidgetItem(f"{var.memory_bytes} B")
            preview_item = QTableWidgetItem(var.value_preview)

            # Colorize type names
            if var.type_name in ("int", "float", "complex", "bool"):
                type_item.setForeground(_qcolor(self._palette.number))
            elif var.type_name in ("str", "bytes"):
                type_item.setForeground(_qcolor(self._palette.string))
            elif var.type_name in ("list", "dict", "tuple", "set", "DataFrame", "ndarray"):
                type_item.setForeground(_qcolor(self._palette.purple))
            elif "function" in var.type_name or "method" in var.type_name:
                type_item.setForeground(_qcolor(self._palette.function_name))

            self._variables_table.setItem(row_idx, 0, name_item)
            self._variables_table.setItem(row_idx, 1, type_item)
            self._variables_table.setItem(row_idx, 2, size_item)
            self._variables_table.setItem(row_idx, 3, mem_item)
            self._variables_table.setItem(row_idx, 4, preview_item)

    @Slot(QProcess.ProcessError)
    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        _LOGGER.warning("REPL subshell error occurred: %s", error)

    @Slot()
    def _on_submit(self) -> None:
        command_text = self._input_field.text()
        if not command_text or self._process is None:
            return
        self._history.append(command_text)
        self._history_index = -1
        self._output_view.appendPlainText(f">>> {command_text}")
        self._process.write(f"{command_text}\n".encode("utf-8"))
        self._input_field.clear()
        self.command_submitted.emit(command_text)

    @Slot(QTableWidgetItem)
    def _on_variable_double_clicked(self, item: QTableWidgetItem) -> None:
        row_idx = item.row()
        target_item = self._variables_table.item(row_idx, 0)
        if target_item:
            var_name = target_item.data(Qt.ItemDataRole.UserRole)
            if var_name:
                self.execute_code(f"print({var_name})")
                self.variable_activated.emit(var_name)


# -----------------------------------------------------------------------------
# Embedded System Terminal Panel (PowerShell / CMD)
# -----------------------------------------------------------------------------


class TerminalPanel(QWidget):
    """An interactive native terminal session (Windows PowerShell / CMD)
    embedded in the bottom dock, pre-configured with the workspace .venv.
    """

    def __init__(
        self,
        palette: ColorPalette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette: ColorPalette = palette
        self._project_root: Path | None = None
        self._process: Optional[QProcess] = None
        self._history: list[str] = []
        self._history_index: int = -1

        self._terminal_view = QPlainTextEdit(self)
        self._terminal_view.setReadOnly(True)
        self._terminal_view.setFont(QFont("Consolas", 10))
        self._terminal_view.setMaximumBlockCount(10000)

        self._input_field = QLineEdit(self)
        self._input_field.setFont(QFont("Consolas", 10))
        self._input_field.setPlaceholderText(
            "Enter terminal command (e.g. uv pip list, pytest, git status)..."
        )
        self._input_field.returnPressed.connect(self._on_submit)
        self._input_field.installEventFilter(self)

        self._title_label = QLabel("💻 Windows PowerShell", self)
        self._title_label.setProperty("role", "section-title")

        self._cwd_label = QLabel("", self)
        self._cwd_label.setStyleSheet(f"color: {palette.muted};")

        clear_btn = QPushButton("🧹 Clear", self)
        clear_btn.clicked.connect(self._terminal_view.clear)

        restart_btn = QPushButton("🔄 Restart", self)
        restart_btn.clicked.connect(self.restart_session)

        send_btn = QPushButton("⏎ Send", self)
        send_btn.setProperty("role", "primary")
        send_btn.clicked.connect(self._on_submit)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self._title_label)
        top_bar.addWidget(self._cwd_label)
        top_bar.addStretch(1)
        top_bar.addWidget(restart_btn)
        top_bar.addWidget(clear_btn)

        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("PS >", self))
        input_row.addWidget(self._input_field, 1)
        input_row.addWidget(send_btn)

        term_box = QGroupBox("💻 Native Terminal", self)
        term_layout = QVBoxLayout(term_box)
        term_layout.addWidget(self._terminal_view)
        term_layout.addLayout(input_row)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(top_bar)
        layout.addWidget(term_box)

        self._apply_colors()
        self.restart_session()

    def set_palette(self, palette: ColorPalette) -> None:
        """Update terminal display colors on theme updates."""
        self._palette = palette
        self._apply_colors()

    def _apply_colors(self) -> None:
        self._terminal_view.setStyleSheet(
            f"background-color: {self._palette.background}; color: {self._palette.text};"
        )

    def set_project_root(self, root: Path | None) -> None:
        """Set project root and restart terminal in that directory."""
        self._project_root = root
        if root:
            self._cwd_label.setText(f"Dir: {root.name}")
        else:
            self._cwd_label.setText("")

    def eventFilter(self, watched_object: QObject, event: QEvent) -> bool:
        """Navigate terminal command history with Up/Down arrow keys."""
        if watched_object is self._input_field and event.type() == QEvent.Type.KeyPress:
            key_event = cast(QKeyEvent, event)
            key = key_event.key()
            if key == Qt.Key.Key_Up:
                if self._history:
                    if self._history_index > 0:
                        self._history_index -= 1
                    elif self._history_index == -1:
                        self._history_index = len(self._history) - 1
                    self._input_field.setText(self._history[self._history_index])
                return True
            elif key == Qt.Key.Key_Down:
                if self._history:
                    if 0 <= self._history_index < len(self._history) - 1:
                        self._history_index += 1
                        self._input_field.setText(self._history[self._history_index])
                    else:
                        self._history_index = -1
                        self._input_field.clear()
                return True
        return super().eventFilter(watched_object, event)

    def restart_session(self) -> None:
        """Spawn a fresh native shell session with project environment variables."""
        if self._process is not None:
            try:
                self._process.kill()
                self._process.waitForFinished(1000)
            except Exception as exc:
                _LOGGER.debug("Terminal cleanup warning: %s", exc)

        self._terminal_view.clear()
        self._process = QProcess(self)

        # Detect platform shell
        if os.name == "nt":
            shell_program = "powershell.exe"
            shell_args = ["-NoLogo", "-NoExit"]
            self._title_label.setText("💻 Windows PowerShell")
        else:
            shell_program = os.environ.get("SHELL", "/bin/bash")
            shell_args = ["-i"]
            self._title_label.setText(f"💻 Terminal ({Path(shell_program).name})")

        self._process.setProgram(shell_program)
        self._process.setArguments(shell_args)

        # Configure environment to prioritize project's .venv
        env = QProcessEnvironment.systemEnvironment()
        if self._project_root:
            self._process.setWorkingDirectory(str(self._project_root))
            venv_scripts = self._project_root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
            if venv_scripts.is_dir():
                sep = ";" if os.name == "nt" else ":"
                env.insert("PATH", f"{venv_scripts}{sep}" + env.value("PATH"))
                env.insert("VIRTUAL_ENV", str(self._project_root / ".venv"))
        else:
            self._process.setWorkingDirectory(str(Path.home()))

        self._process.setProcessEnvironment(env)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._on_terminal_output)
        self._process.start()

    @Slot()
    def _on_terminal_output(self) -> None:
        if self._process is None:
            return
        data = bytes(self._process.readAllStandardOutput().data()).decode(
            "utf-8", errors="replace"
        )
        self._terminal_view.appendPlainText(data.rstrip("\r\n"))

    @Slot()
    def _on_submit(self) -> None:
        cmd = self._input_field.text()
        if not cmd or self._process is None:
            return
        self._history.append(cmd)
        self._history_index = -1
        self._terminal_view.appendPlainText(f"> {cmd}")
        line_ending = "\r\n" if os.name == "nt" else "\n"
        self._process.write(f"{cmd}{line_ending}".encode("utf-8"))
        self._input_field.clear()

    def cleanup(self) -> None:
        """Terminate and clean up active subprocess."""
        if self._process is not None:
            try:
                self._process.kill()
                self._process.waitForFinished(1000)
            except Exception:
                pass
            self._process = None



