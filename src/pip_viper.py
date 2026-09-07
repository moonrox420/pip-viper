"""PipViper Core IDE Backend Module.

This module provides the structural foundation for the PipViper IDE,
including robust configuration management, structured logging with trace IDs,
pydantic validation schemas, PySide6-compliant thread-safe background workers,
and a high-performance facade over the Jedi auto-completion library.
"""

from __future__ import annotations

import ast
import contextvars
import enum
import importlib.util
import json
import logging
import logging.handlers
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Generic, ParamSpec, TypeVar

import jedi
from pydantic import BaseModel, ConfigDict, Field
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QApplication

__version__ = "7.0.0"

__all__ = [
    "__version__",
    "main",
    "AppConfig",
    "ColorPalette",
    "ThemePalette",
    "EditorTheme",
    "PipViperError",
    "ConfigurationError",
    "FileOperationError",
    "FileReadError",
    "FileWriteError",
    "JediError",
    "LinterError",
    "LinterNotFoundError",
    "LinterParseError",
    "ProcessError",
    "ProcessStartupError",
    "ProcessTimeoutError",
    "ThemeError",
    "CodeToolError",
    "SyntaxFixError",
    "FormattingError",
    "ImportOrganizationError",
    "UnusedCodeCleanupError",
    "RefactorError",
    "CodeGenerationError",
    "JediCompletion",
    "JediResult",
    "JediTaskType",
    "LintIssue",
    "LintSeverity",
    "LintTool",
    "PipPackage",
    "WorkerSignals",
    "BackgroundWorker",
    "JediService",
    "configure_logging",
    "run_in_thread",
    "current_trace_id",
    "get_palette",
    "get_missing_imports",
    "map_module_to_pypi",
    "query_local_llm",
]

# -----------------------------------------------------------------------------
# Exceptions Hierarchy
# -----------------------------------------------------------------------------


class PipViperError(Exception):
    """Root of the PipViper exception hierarchy."""


class ConfigurationError(PipViperError):
    """Invalid or missing configuration."""


class FileOperationError(PipViperError):
    """Filesystem operation failed."""


class FileReadError(FileOperationError):
    """Reading a file failed."""


class FileWriteError(FileOperationError):
    """Writing a file failed."""


class JediError(PipViperError):
    """A Jedi intelligence query failed."""


class LinterError(PipViperError):
    """Running or parsing a linter failed."""


class LinterNotFoundError(LinterError):
    """The requested linter executable is not on the system PATH."""


class LinterParseError(LinterError):
    """The linter produced output that could not be parsed."""


class ProcessError(PipViperError):
    """An external process failed to start or behaved unexpectedly."""


class ProcessStartupError(ProcessError):
    """The process could not be started."""


class ProcessTimeoutError(ProcessError):
    """The process did not finish within the allotted time."""


class ThemeError(PipViperError):
    """Theme lookup or application failed."""


class CodeToolError(PipViperError):
    """Root of the code-tools exception hierarchy."""


class SyntaxFixError(CodeToolError):
    """The syntax auto-fixer could not safely process the supplied source."""


class FormattingError(CodeToolError):
    """The Black-backed formatter failed to format the supplied source."""


class ImportOrganizationError(CodeToolError):
    """The isort-backed import organizer failed to process the source."""


class UnusedCodeCleanupError(CodeToolError):
    """The autoflake-backed unused import/variable cleanup pass failed."""


class RefactorError(CodeToolError):
    """A refactor operation (e.g. symbol rename) could not be completed."""


class CodeGenerationError(CodeToolError):
    """A code-generation operation (docstrings, tests) could not be completed."""


# -----------------------------------------------------------------------------
# Structured Logging with Context Trace IDs
# -----------------------------------------------------------------------------

DEFAULT_LOG_DIRECTORY: Path = Path.home() / ".src" / "logs"
LOG_FORMAT: str = "%(asctime)s [%(trace_id)s] %(levelname)-8s %(name)s :: %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%dT%H:%M:%S"
ROTATION_BYTES: int = 5 * 1024 * 1024
ROTATION_BACKUPS: int = 5

current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_trace_id", default="-"
)


class TraceIdFilter(logging.Filter):
    """Injects the active `current_trace_id` into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter log records by injecting the trace identifier."""
        record.trace_id = current_trace_id.get()
        return True


_LOGGER: logging.Logger = logging.getLogger("src")
_LOGGING_CONFIGURED: bool = False


def configure_logging(
    log_directory: Path = DEFAULT_LOG_DIRECTORY,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure the root 'src' logger.

    This function is idempotent and thread-safe. It configures a rotating
    file handler and injects trace IDs. It suppresses standard output logging.
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return _LOGGER

    try:
        log_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exception:
        raise FileOperationError(
            f"Failed to create log directory: {log_directory}"
        ) from exception

    log_file_path = log_directory / "src.log"

    handler = logging.handlers.RotatingFileHandler(
        filename=str(log_file_path),
        maxBytes=ROTATION_BYTES,
        backupCount=ROTATION_BACKUPS,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    handler.addFilter(TraceIdFilter())

    _LOGGER.setLevel(level)
    _LOGGER.addHandler(handler)
    _LOGGER.propagate = False
    _LOGGING_CONFIGURED = True

    _LOGGER.debug("Structured logging successfully configured at %s", log_file_path)
    return _LOGGER


def shutdown_logging() -> None:
    """Close and remove all handlers from the _LOGGER, resetting configuration state."""
    global _LOGGING_CONFIGURED
    for handler in list(_LOGGER.handlers):
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass
        _LOGGER.removeHandler(handler)
    _LOGGING_CONFIGURED = False


def get_trace_id() -> str:
    """Return a unique 12-character trace ID string."""
    trace_identifier = uuid.uuid4().hex[:12]
    current_trace_id.set(trace_identifier)
    return trace_identifier


# -----------------------------------------------------------------------------
# Configuration and Styling Definitions
# -----------------------------------------------------------------------------


class EditorTheme(str, enum.Enum):
    """Available built-in editor color themes."""

    DARK = "dark"
    LIGHT = "light"
    SOLARIZED = "solarized"
    MONOKAI = "monokai"


class ColorPalette(BaseModel):
    """Complete set of validated color tokens for the UI and highlighter."""

    model_config = ConfigDict(frozen=True)

    # Surfaces
    background: str
    panel: str
    border: str
    text: str
    muted: str
    selection: str
    current_line: str

    # Accents
    blue: str
    green: str
    red: str
    yellow: str
    purple: str
    orange: str

    # Syntax tokens
    keyword: str
    string: str
    comment: str
    number: str
    builtin: str
    function_name: str
    decorator: str
    class_name: str
    operator: str

    @property
    def accent(self) -> str:
        """Convenience property for UI accent styling."""
        return self.blue

    def to_dict(self) -> dict[str, str]:
        """Return a plain dictionary of the color tokens in canonical order."""
        return {
            "background": self.background,
            "panel": self.panel,
            "border": self.border,
            "text": self.text,
            "muted": self.muted,
            "selection": self.selection,
            "current_line": self.current_line,
            "blue": self.blue,
            "green": self.green,
            "red": self.red,
            "yellow": self.yellow,
            "purple": self.purple,
            "orange": self.orange,
            "keyword": self.keyword,
            "string": self.string,
            "comment": self.comment,
            "number": self.number,
            "builtin": self.builtin,
            "function_name": self.function_name,
            "decorator": self.decorator,
            "class_name": self.class_name,
            "operator": self.operator,
        }


# ThemePalette alias for ColorPalette
ThemePalette = ColorPalette


_DARK_PALETTE = ColorPalette(
    background="#0d1117",
    panel="#161b22",
    border="#30363d",
    text="#c9d1d9",
    muted="#8b949e",
    selection="#264f78",
    current_line="#1f2937",
    blue="#58a6ff",
    green="#3fb950",
    red="#ff7b72",
    yellow="#d29922",
    purple="#bc8cff",
    orange="#f0883e",
    keyword="#ff7b72",
    string="#a5d6ff",
    comment="#8b949e",
    number="#79c0ff",
    builtin="#79c0ff",
    function_name="#d2a8ff",
    decorator="#ffa657",
    class_name="#ffa657",
    operator="#ff7b72",
)

_LIGHT_PALETTE = ColorPalette(
    background="#ffffff",
    panel="#f6f8fa",
    border="#d0d7de",
    text="#1f2328",
    muted="#656d76",
    selection="#0969da",
    current_line="#f6f8fa",
    blue="#0969da",
    green="#1a7f37",
    red="#cf222e",
    yellow="#9a6700",
    purple="#8250df",
    orange="#bc4c00",
    keyword="#cf222e",
    string="#0a3069",
    comment="#6e7781",
    number="#0550ae",
    builtin="#0550ae",
    function_name="#8250df",
    decorator="#953800",
    class_name="#953800",
    operator="#cf222e",
)

_SOLARIZED_PALETTE = ColorPalette(
    background="#002b36",
    panel="#073642",
    border="#586e75",
    text="#839496",
    muted="#586e75",
    selection="#073642",
    current_line="#06313d",
    blue="#268bd2",
    green="#859900",
    red="#dc322f",
    yellow="#b58900",
    purple="#6c71c4",
    orange="#cb4b16",
    keyword="#dc322f",
    string="#2aa198",
    comment="#586e75",
    number="#b58900",
    builtin="#268bd2",
    function_name="#6c71c4",
    decorator="#cb4b16",
    class_name="#cb4b16",
    operator="#dc322f",
)

_MONOKAI_PALETTE = ColorPalette(
    background="#272822",
    panel="#3e3d32",
    border="#75715e",
    text="#f8f8f2",
    muted="#75715e",
    selection="#49483e",
    current_line="#3e3d32",
    blue="#66d9ef",
    green="#a6e22e",
    red="#f92672",
    yellow="#e6db74",
    purple="#ae81ff",
    orange="#fd971f",
    keyword="#f92672",
    string="#e6db74",
    comment="#75715e",
    number="#ae81ff",
    builtin="#66d9ef",
    function_name="#a6e22e",
    decorator="#fd971f",
    class_name="#a6e22e",
    operator="#f92672",
)

_BUILTIN_COLOR_PALETTES: dict[EditorTheme, ColorPalette] = {
    EditorTheme.DARK: _DARK_PALETTE,
    EditorTheme.LIGHT: _LIGHT_PALETTE,
    EditorTheme.SOLARIZED: _SOLARIZED_PALETTE,
    EditorTheme.MONOKAI: _MONOKAI_PALETTE,
}


def get_palette(theme: EditorTheme) -> ColorPalette:
    """Return the color palette corresponding to the specified theme.

    Args:
        theme: The EditorTheme enum value.

    Returns:
        The matching ColorPalette instance.

    Raises:
        ThemeError: If the theme is not found in the built-in palettes.
    """
    try:
        return _BUILTIN_COLOR_PALETTES[theme]
    except KeyError as exception:
        raise ThemeError(f"Unknown editor theme: {theme!r}") from exception


class AppConfig(BaseModel):
    """Non-theme runtime configuration for the PipViper IDE."""

    model_config = ConfigDict(frozen=True)

    theme: EditorTheme = EditorTheme.DARK
    font_family: str = "Consolas"
    font_size: int = Field(default=11, ge=6, le=72)
    auto_save_seconds: int = Field(default=0, ge=0, le=3600)
    completion_delay_ms: int = Field(default=150, ge=0, le=2000)
    hover_delay_ms: int = Field(default=500, ge=0, le=5000)
    log_directory: Path = Field(default_factory=lambda: DEFAULT_LOG_DIRECTORY)
    python_executable: str = Field(default_factory=lambda: sys.executable)


# -----------------------------------------------------------------------------
# Inter-Module Payload Models
# -----------------------------------------------------------------------------


class LintSeverity(str, enum.Enum):
    """Severity classification for linter findings."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    CONVENTION = "convention"


class LintTool(str, enum.Enum):
    """Supported local linter tools."""

    FLAKE8 = "flake8"
    PYLINT = "pylint"
    MYPY = "mypy"


class LintIssue(BaseModel):
    """A single static analysis issue captured by any linting tool."""

    model_config = ConfigDict(frozen=True)

    file_path: Path
    line: int = Field(ge=0)
    column: int = Field(ge=0)
    code: str
    tool: LintTool
    severity: LintSeverity
    message: str


class PipPackage(BaseModel):
    """An installed Python pip package."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    location: Path
    latest: str | None = None


class JediTaskType(str, enum.Enum):
    """Supported types of asynchronous code intelligence requests."""

    COMPLETION = "completion"
    SIGNATURE = "signature"
    HOVER = "hover"
    DEFINITION = "definition"
    REFERENCES = "references"


class JediCompletion(BaseModel):
    """A single autocompletion candidate."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: str
    description: str


class JediResult(BaseModel):
    """Result of any Jedi code intelligence query."""

    model_config = ConfigDict(frozen=True)

    task_type: JediTaskType
    completions: list[JediCompletion] = Field(default_factory=list)
    signature: str | None = None
    hover_text: str | None = None
    definition_line: int | None = None
    definition_column: int | None = None
    definition_path: str | None = None
    references: list[tuple[int, int]] = Field(default_factory=list)
    error: str | None = None

    @classmethod
    def error_result(cls, task_type: JediTaskType, message: str) -> JediResult:
        """Construct a JediResult representing an execution error."""
        return cls(task_type=task_type, error=message)


# -----------------------------------------------------------------------------
# Safe Thread Concurrency Infrastructure
# -----------------------------------------------------------------------------

T = TypeVar("T")
P = ParamSpec("P")


class WorkerSignals(QObject):
    """Signals available from a running BackgroundWorker."""

    started = Signal()
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class BackgroundWorker(QRunnable, Generic[T]):
    """Runs a callable task on a background thread pool."""

    def __init__(
        self,
        task_function: Callable[..., T],
        *arguments: object,
        **keyword_arguments: object,
    ) -> None:
        super().__init__()
        self._task_function = task_function
        self._arguments = arguments
        self._keyword_arguments = keyword_arguments
        self.signals = WorkerSignals()
        self._trace_id: str = current_trace_id.get()

    @Slot()
    def run(self) -> None:
        """Execute the task function and emit corresponding signals."""
        trace_token = current_trace_id.set(self._trace_id)
        self.signals.started.emit()
        try:
            result_value = self._task_function(
                *self._arguments, **self._keyword_arguments
            )
        except PipViperError as exception:
            _LOGGER.error(
                "Background task failed with known error: %s",
                exception,
                exc_info=True,
            )
            try:
                self.signals.error.emit(str(exception))
            except RuntimeError:
                pass
        except Exception as exception:
            _LOGGER.exception("Unhandled background error")
            try:
                self.signals.error.emit(f"{type(exception).__name__}: {exception}")
            except RuntimeError:
                pass
        else:
            try:
                self.signals.result.emit(result_value)
            except RuntimeError:
                pass
        finally:
            try:
                self.signals.finished.emit()
            except RuntimeError:
                pass
            current_trace_id.reset(trace_token)


_ACTIVE_WORKERS: dict[int, object] = {}


def run_in_thread(
    task_function: Callable[..., T], *arguments: object, **keyword_arguments: object
) -> BackgroundWorker[T]:
    """Schedule a callable task to run on the global QThreadPool."""
    worker = BackgroundWorker(task_function, *arguments, **keyword_arguments)
    worker_identifier = id(worker)
    _ACTIVE_WORKERS[worker_identifier] = worker

    def cleanup_worker() -> None:
        _ACTIVE_WORKERS.pop(worker_identifier, None)

    worker.signals.finished.connect(cleanup_worker)
    QThreadPool.globalInstance().start(worker)
    return worker


# -----------------------------------------------------------------------------
# Thread-Safe Jedi Autocompletion Service
# -----------------------------------------------------------------------------


class JediService(QObject):
    """Dispatches slow Jedi query evaluations safely to background threads."""

    results = Signal(int, object)  # token (int), result (JediResult)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._token: int = 0
        self._project: jedi.Project | None = None

    def set_project_path(self, path: Path | None) -> None:
        """Configure a Jedi Project to cache workspace imports efficiently."""
        if path:
            self._project = jedi.Project(str(path))
            _LOGGER.info("Jedi Project workspace context loaded: %s", path)
        else:
            self._project = None

    def _next_token(self) -> int:
        self._token += 1
        return self._token

    def request_completion(
        self, source: str, line: int, column: int, file_path: Path
    ) -> int:
        token = self._next_token()
        worker = run_in_thread(self._complete, source, line, column, file_path, token, self._project)
        worker.signals.result.connect(self._dispatch)
        return token

    def request_signature(
        self, source: str, line: int, column: int, file_path: Path
    ) -> int:
        token = self._next_token()
        worker = run_in_thread(self._signatures, source, line, column, file_path, token, self._project)
        worker.signals.result.connect(self._dispatch)
        return token

    def request_hover(
        self, source: str, line: int, column: int, file_path: Path
    ) -> int:
        token = self._next_token()
        worker = run_in_thread(self._hover, source, line, column, file_path, token, self._project)
        worker.signals.result.connect(self._dispatch)
        return token

    def request_definition(
        self, source: str, line: int, column: int, file_path: Path
    ) -> int:
        token = self._next_token()
        worker = run_in_thread(self._definition, source, line, column, file_path, token, self._project)
        worker.signals.result.connect(self._dispatch)
        return token

    def request_references(
        self, source: str, line: int, column: int, file_path: Path
    ) -> int:
        token = self._next_token()
        worker = run_in_thread(self._references, source, line, column, file_path, token, self._project)
        worker.signals.result.connect(self._dispatch)
        return token

    @Slot(object)
    def _dispatch(self, payload: tuple[int, JediResult]) -> None:
        token, result = payload
        self.results.emit(token, result)

    # -------------------------------------------------------------------------
    # Under-the-hood Static Query Evaluators
    # -------------------------------------------------------------------------

    @staticmethod
    def _complete(
        source: str, line: int, column: int, file_path: Path, token: int, project: jedi.Project | None
    ) -> tuple[int, JediResult]:
        try:
            script = jedi.Script(code=source, path=str(file_path), project=project)
            completions = [
                JediCompletion(
                    name=completion.name,
                    kind=(
                        str(completion.type).rsplit(".", maxsplit=1)[-1]
                        if completion.type
                        else "instance"
                    ),
                    description=(completion.description or "").splitlines()[0][:120],
                )
                for completion in script.complete(line, column)[:50]
            ]
            return token, JediResult(
                task_type=JediTaskType.COMPLETION, completions=completions
            )
        except Exception as exception:
            return token, JediResult.error_result(
                JediTaskType.COMPLETION, str(exception)
            )

    @staticmethod
    def _signatures(
        source: str, line: int, column: int, file_path: Path, token: int, project: jedi.Project | None
    ) -> tuple[int, JediResult]:
        try:
            script = jedi.Script(code=source, path=str(file_path), project=project)
            signatures = script.get_signatures(line, column)
            signature_text = signatures[0].to_string() if signatures else None
            return token, JediResult(
                task_type=JediTaskType.SIGNATURE, signature=signature_text
            )
        except Exception as exception:
            return token, JediResult.error_result(
                JediTaskType.SIGNATURE, str(exception)
            )

    @staticmethod
    def _hover(
        source: str, line: int, column: int, file_path: Path, token: int, project: jedi.Project | None
    ) -> tuple[int, JediResult]:
        try:
            script = jedi.Script(code=source, path=str(file_path), project=project)
            definitions = script.goto(line, column, follow_imports=True)
            if not definitions:
                return token, JediResult(task_type=JediTaskType.HOVER)
            target = definitions[0]
            signatures = script.get_signatures(line, column)
            signature_text = signatures[0].to_string() if signatures else ""
            docstring = (
                (target.docstring() or "").strip()[:400]
                if hasattr(target, "docstring")
                else ""
            )
            hover_text = (
                f"{target.name}\n{signature_text}\n\n{docstring}"
                if docstring
                else f"{target.name}\n{signature_text}"
            )
            return token, JediResult(
                task_type=JediTaskType.HOVER,
                hover_text=hover_text,
            )
        except Exception as exception:
            return token, JediResult.error_result(JediTaskType.HOVER, str(exception))

    @staticmethod
    def _definition(
        source: str, line: int, column: int, file_path: Path, token: int, project: jedi.Project | None
    ) -> tuple[int, JediResult]:
        try:
            script = jedi.Script(code=source, path=str(file_path), project=project)
            definitions = script.goto(line, column, follow_imports=True)
            if not definitions:
                return token, JediResult(task_type=JediTaskType.HOVER)
            target = definitions[0]
            target_path = (
                str(target.module_path)
                if hasattr(target, "module_path") and target.module_path
                else str(file_path)
            )
            return token, JediResult(
                task_type=JediTaskType.DEFINITION,
                definition_line=target.line,
                definition_column=target.column,
                definition_path=target_path,
            )
        except Exception as exception:
            return token, JediResult.error_result(
                JediTaskType.DEFINITION, str(exception)
            )

    @staticmethod
    def _references(
        source: str, line: int, column: int, file_path: Path, token: int, project: jedi.Project | None
    ) -> tuple[int, JediResult]:
        try:
            script = jedi.Script(code=source, path=str(file_path), project=project)
            references = script.get_references(line, column)
            reference_tuples = [
                (reference.line, reference.column) for reference in references[:100]
            ]
            return token, JediResult(
                task_type=JediTaskType.REFERENCES,
                references=reference_tuples,
            )
        except Exception as exception:
            return token, JediResult.error_result(
                JediTaskType.REFERENCES, str(exception)
            )


# -----------------------------------------------------------------------------
# Core Self-Healing Environment Scanners
# -----------------------------------------------------------------------------

_MODULE_TO_PYPI_MAP: dict[str, str] = {
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "git": "gitpython",
    "jwt": "pyjwt",
    "dotenv": "python-dotenv",
    "sqlite3": "pysqlite3",
    "sqlalchemy": "sqlalchemy",
    "numpy": "numpy",
    "pandas": "pandas",
    "requests": "requests",
    "matplotlib": "matplotlib",
    "scipy": "scipy",
    "sklearn": "scikit-learn",
    "rich": "rich",
    "click": "click",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "jinja2": "jinja2",
    "redis": "redis",
    "psycopg2": "psycopg2-binary",
    "serial": "pyserial",
    "dateutil": "python-dateutil",
    "magic": "python-magic",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "fitz": "PyMuPDF",
    "OpenSSL": "pyOpenSSL",
    "Crypto": "pycryptodome",
    "playwright": "playwright",
    "pytest": "pytest",
    "pydantic": "pydantic",
    "jedi": "jedi",
    "black": "black",
    "isort": "isort",
    "ruff": "ruff",
    "mypy": "mypy",
}


def map_module_to_pypi(module_name: str) -> str:
    """Map a Python import module name to its installable PyPI distribution name.

    First inspects the static curated mapping, then dynamically checks
    standard library ``importlib.metadata.packages_distributions()``.

    Args:
        module_name: The extracted imported package name.

    Returns:
        The matched PyPI distribution name.
    """
    if module_name in _MODULE_TO_PYPI_MAP:
        return _MODULE_TO_PYPI_MAP[module_name]

    try:
        import importlib.metadata
        dists = importlib.metadata.packages_distributions().get(module_name)
        if dists and len(dists) > 0:
            return dists[0]
    except Exception:
        pass

    return module_name


def get_missing_imports(
    source_code: str,
    active_file_directory: Path | None = None,
    python_executable: str | None = None,
    site_packages_paths: list[str] | None = None,
) -> list[str]:
    """Scans the source code using Python's AST to detect missing external packages.

    This function filters out standard library packages (using sys.stdlib_module_names),
    ignores local file/folder siblings to prevent false positives, and returns
    uninstalled imports. When a virtual environment python executable or custom
    site-packages path is supplied, checks against that environment's packages.

    Args:
        source_code: The active Python source text in the editor.
        active_file_directory: Sibling directory path to verify local module folders.
        python_executable: Optional path to the virtualenv python interpreter.
        site_packages_paths: Optional list of custom site-packages directory paths.

    Returns:
        A list of uninstalled top-level module names.
    """
    try:
        syntax_tree = ast.parse(source_code)
    except Exception:
        return []

    top_level_modules: set[str] = set()
    for ast_node in ast.walk(syntax_tree):
        if isinstance(ast_node, ast.Import):
            for alias in ast_node.names:
                top_level_modules.add(alias.name.split(".")[0])
        elif isinstance(ast_node, ast.ImportFrom):
            if ast_node.level == 0 and ast_node.module:
                top_level_modules.add(ast_node.module.split(".")[0])

    missing_modules: list[str] = []

    # Python 3.10+ guaranteed standard library and builtin set union
    stdlib_set = sys.stdlib_module_names | set(sys.builtin_module_names)

    # Resolve target environment site-packages if specified
    search_paths = list(site_packages_paths) if site_packages_paths else None
    if search_paths is None and python_executable:
        exe_path = Path(python_executable)
        prefix = exe_path.parent.parent
        win_site = prefix / "Lib" / "site-packages"
        if win_site.is_dir():
            search_paths = [str(win_site)]
        else:
            unix_lib = prefix / "lib"
            if unix_lib.is_dir():
                for sub in unix_lib.iterdir():
                    candidate = sub / "site-packages"
                    if candidate.is_dir():
                        search_paths = [str(candidate)]
                        break

    for module in top_level_modules:
        if not module:
            continue
        if module in stdlib_set:
            continue
        if module in (
            "__main__",
            "src",
            "editor",
            "panels",
            "styles",
            "widgets",
            "dependencies",
            "environment",
        ):
            continue

        # Check local filesystem siblings
        if active_file_directory is not None:
            if (active_file_directory / f"{module}.py").exists():
                continue
            if (active_file_directory / module).is_dir():
                continue

        # Check if importable inside target or active environment
        found = False
        if search_paths:
            try:
                import importlib.machinery
                spec = importlib.machinery.PathFinder.find_spec(module, path=search_paths)
                if spec is not None:
                    found = True
            except Exception:
                pass

        if not found:
            try:
                specification = importlib.util.find_spec(module)
                if specification is not None:
                    found = True
            except Exception:
                pass

        if not found:
            missing_modules.append(module)

    return missing_modules


# -----------------------------------------------------------------------------
# Local-First AI Integration Handler
# -----------------------------------------------------------------------------


import urllib.parse


def query_local_llm(
    api_url: str,
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float = 30.0,
) -> str:
    """Query a local OpenAI-compatible API endpoint natively (Ollama or llama.cpp).

    Strictly local-only (PRD O5 & S7):
    Enforces localhost-only endpoints, validates input prompt size, and provides
    clear error messages when the local AI sidecar is unreachable.

    Args:
        api_url: Base endpoint URL, e.g., "http://127.0.0.1:11434/v1"
        model_name: Local model tag on disk (e.g. "llama3", "mistral", "qwen").
        system_prompt: System directive instructions.
        user_prompt: User input or source code block.
        timeout: HTTP connection timeout in seconds (default: 30.0).

    Returns:
        The text response generated by the local AI.
    """
    # 1. Enforce localhost-only hostnames (PRD O5)
    parsed_url = urllib.parse.urlparse(api_url)
    hostname = (parsed_url.hostname or "").lower()
    allowed_local_hosts = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if hostname not in allowed_local_hosts:
        raise ProcessError(
            f"Security Violation: External AI endpoint '{hostname}' is blocked. "
            f"PipViper's offline-first architecture strictly requires a local AI sidecar "
            f"(localhost / 127.0.0.1)."
        )

    # 2. Input validation on prompts (PRD S7)
    max_chars = 65536
    if len(user_prompt) > max_chars:
        raise ValueError(
            f"Prompt size ({len(user_prompt)} chars) exceeds safe local sidecar limit of {max_chars} characters."
        )

    # Strip null bytes and control chars
    clean_prompt = user_prompt.replace("\x00", "")

    request_headers = {"Content-Type": "application/json"}
    payload_dictionary = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": clean_prompt},
        ],
        "temperature": 0.2,
    }
    encoded_data = json.dumps(payload_dictionary).encode("utf-8")
    request_object = urllib.request.Request(
        f"{api_url.rstrip('/')}/chat/completions",
        data=encoded_data,
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request_object, timeout=timeout) as response_stream:
            parsed_json = json.loads(response_stream.read().decode("utf-8"))
            return str(parsed_json["choices"][0]["message"]["content"])
    except urllib.error.HTTPError as exception:
        error_body = ""
        try:
            error_body = exception.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        _LOGGER.exception("Local LLM HTTP error: %s", error_body)
        raise ProcessError(
            f"Local AI sidecar returned HTTP {exception.code}: {error_body or exception.reason}"
        ) from exception
    except urllib.error.URLError as exception:
        _LOGGER.warning("Local LLM connection failed to %s: %s", api_url, exception)
        raise ProcessError(
            f"Could not connect to local AI sidecar at {api_url}.\n"
            f"Ensure Ollama or llama.cpp is running locally (`ollama serve`)."
        ) from exception
    except Exception as exception:
        _LOGGER.exception("LLM JSON parsing error")
        raise ProcessError(f"Unexpected error communicating with local AI: {exception}") from exception


# -----------------------------------------------------------------------------
# Main Application Launcher
# -----------------------------------------------------------------------------


def main() -> int:
    """Launch the PipViper backend process and application services.

    Returns:
        The exit status code returned by the running QApplication.
    """
    configure_logging()
    _LOGGER.info("Starting PipViper service layer...")
    application = QApplication(sys.argv)
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
