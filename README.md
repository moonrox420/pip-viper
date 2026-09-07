# PipViper IDE 🐍

PipViper is a modern, high-performance, **offline-first** Python Integrated Development Environment (IDE) built on PySide6 and standard Python libraries. Designed for absolute privacy, zero silent network calls, and local-first execution, PipViper operates completely disconnected from external servers by default.

---

## 🔒 True Offline-First Architecture

PipViper enforces a strict **Offline-First Non-Negotiable Principle**:
* **Zero silent network calls**: No telemetry, no automatic update pinging, and no hidden tracking.
* **Global Offline Mode**: Enabled by default on first launch. Every network path is hard-disabled until explicitly opted into.
* **Local AI Only**: AI features communicate strictly with local sidecars (e.g., Ollama or llama.cpp on `127.0.0.1`). External cloud model endpoints are rejected.

### Capabilities Matrix

| Feature Domain | Offline Status | Operational Details |
| :--- | :--- | :--- |
| **Core Editing & Folding** | **100% Offline** | Syntax highlighting, code folding, bracket matching, indentation engines. |
| **Code Navigation & Jedi** | **100% Offline** | Autocomplete, parameter hints, hover docs, and jump-to-definition via local `jedi`. |
| **Script Execution & REPL** | **100% Offline** | Run scripts, interact with the persistent subshell, and stream stdout/stderr locally. |
| **Visual Debugger** | **100% Offline** | Interactive stepping, call stack inspection, variables, and breakpoints via `debugpy`. |
| **Testing Engine** | **100% Offline** | Pytest test discovery, single/file/suite runner, live gutter status badges. |
| **Linting & Code Tools** | **100% Offline** | Ruff, Mypy, Flake8, Black, isort, autoflake, AST syntax auto-fixer, docstring generator. |
| **Internals & Profiling** | **100% Offline** | AST visualizer, bytecode disassembler, symtable explorer, cProfile flame chart, memory meter. |
| **Git Source Control** | **100% Offline** | Stage/unstage, commits, branches, side-by-side diff viewer, gutter change markers (local repo). |
| **Dependency Scanning** | **100% Offline** | Analyzes AST imports and local `site-packages` metadata with zero PyPI calls. |
| **Local AI Assistant** | **Requires Local Sidecar** | Queries local Ollama / llama.cpp instance (`http://127.0.0.1:11434`). Never hits external APIs. |
| **Package Installs / Updates** | **Optional / Gated** | Blocked in Offline Mode. When Online Mode is opted into, installs packages via `uv`/`pip`. Supports local wheelhouse (`--find-links`). |

---

## 🌟 Key Highlights

* **Deterministic & Local Semantic Code Repair**:
  * **Deterministic (Code Tools)**: Rule-based syntax auto-fixer that repairs colons, brackets, tabs/spaces, unclosed strings, and print syntax natively without network.
  * **Semantic (Local AI Sidecar)**: Optional generative refactoring powered by local LLMs (e.g., `qwen2.5-coder:7b`) with mandatory side-by-side diff review before applying changes.
* **Smart Workspace & Environment Discovery**:
  * Automatically discovers virtual environments (`.venv`, Conda, Poetry, Pipenv, system Python) in the active project.
  * Allows dynamic switching of runtime interpreters without restarting the IDE.
* **Process & Security Hardening**:
  * Shell metacharacter validation and argument sanitization on all subprocess executions.
  * Process tree termination (`taskkill /F /T` / `os.kill`) guarantees cancel buttons kill long-running processes.
  * Sanitized process environment prevents leakage of sensitive host environment variables.
* **Persistent Status Indicators**:
  * Permanent **Offline Mode** status badge (`🔒 Offline Mode` / `🌐 Online (Opt-in)`).
  * Real-time memory meter, active Git branch, cursor coordinates, and active interpreter badge.

---

## 📂 Project Structure

```text
pip-viper/
├── src/
│   ├── app.py                     # Main orchestrator window shell
│   ├── code_tools.py              # Rule-based syntax fixer, formatters, docstrings
│   ├── debug_harness.py           # IPC JSON debug harness wrapper
│   ├── dependencies.py            # Local AST dependency graph and requirements scanner
│   ├── diagnostics.py             # Ruff and Mypy integration services
│   ├── diff_viewer.py             # Side-by-side graphical diff inspection dialog
│   ├── editor.py                  # CodeEditor canvas, syntax highlighter, line gutter
│   ├── environment.py             # Virtualenv detector and interpreter selector
│   ├── internals.py               # AST, bytecode, symtable, cProfile engines
│   ├── memory_tracker.py          # Real-time process memory telemetry
│   ├── navigation.py              # Quick Open (Ctrl+P) and Search in Files
│   ├── pip_viper.py               # Core configuration, models, threading, logging
│   ├── profiler_visualizer.py     # Execution profiler visualization widget
│   ├── repl_harness.py            # Subshell harness
│   ├── styles.py                  # Dark / Light / Monokai flat QSS themes
│   ├── testing.py                 # Pytest runner engine and discovery
│   ├── vcs.py                     # Local Git service wrapper
│   ├── widgets.py                 # File explorer, document outline, memory meter, offline badge
│   ├── controllers/               # Decomposed UI controllers (A1)
│   │   ├── ai_controller.py
│   │   ├── editor_controller.py
│   │   ├── environment_controller.py
│   │   ├── layout_controller.py
│   │   ├── package_controller.py
│   │   ├── run_debug_controller.py
│   │   └── status_bar_controller.py
│   ├── panels/                    # Modular bottom-dock panels (A2)
│   │   ├── ai_panel.py
│   │   ├── code_tools_panel.py
│   │   ├── debug_panel.py
│   │   ├── dependency_studio_panel.py
│   │   ├── git_panel.py
│   │   ├── internals_panel.py
│   │   ├── lint_widget.py
│   │   ├── log_panel.py
│   │   ├── output_panel.py
│   │   ├── package_manager_widget.py
│   │   ├── repl_panel.py
│   │   └── test_runner_panel.py
│   └── services/                  # Explicit service layer & composition root (A3, A5)
│       ├── ai_service.py
│       ├── code_tools_service.py
│       ├── container.py
│       ├── diagnostics_service.py
│       ├── environment_service.py
│       ├── git_service.py
│       ├── jedi_service.py
│       ├── linter_service.py
│       ├── offline_service.py
│       └── process_service.py
├── desktop_packaging/             # PyInstaller standalone build & smoke test scripts
├── tests/                         # Comprehensive 210+ test suite (100% offline)
└── pyproject.toml                 # Package definition and dependencies
```

---

## 🚀 Installation & Setup

### Prerequisites
* **Python**: 3.10 or newer.
* **PySide6**, **pydantic**, **jedi**.

### Development Install
```bash
# Clone the repository
git clone https://github.com/moonrox420/pip-viper.git
cd pip-viper

# Create and activate virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Unix/macOS:
source .venv/bin/activate

# Install dependencies and editable package
pip install -e .
```

---

## 💻 Launching the IDE

```bash
# Method A: Via installed CLI entrypoint
pip-viper

# Method B: Direct module launch
python -m src

# Method C: Standalone launcher script
python launcher.py
```

---

## 🤖 Local AI Setup (Ollama / Local Sidecar)

PipViper does not connect to external AI services (no OpenAI, Anthropic, or remote API keys). To use the AI Assistant:

1. Install [Ollama](https://ollama.ai/) locally.
2. Start the Ollama server:
   ```bash
   ollama serve
   ```
3. Pull your preferred coding model:
   ```bash
   ollama pull qwen2.5-coder:7b
   # or for lightweight setups:
   ollama pull qwen2.5-coder:1.5b
   ```
4. Open the right-side **AI Assistant** panel in PipViper, set your model tag (`qwen2.5-coder:7b`), and enter your prompt.
5. Any code suggested by the AI will be presented in a **Side-by-Side Diff Review Dialog** for your inspection before any file on disk is modified.

---

## 🧪 Running the Test Suite

PipViper includes a comprehensive automated test suite verifying offline behavior, process cancellation, controllers, and services:

```bash
pytest -v
```

To run packaging smoke tests and release artifact hygiene checks:
```bash
python desktop_packaging/build_standalone.py --smoke-test --validate
```

---

## 📜 License

MIT License. Copyright (c) 2026 Dustin Hill. See `LICENSE` for details.