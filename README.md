# PipViper IDE 🐍

PipViper is a modern, high-performance, and offline-first Python Integrated Development Environment (IDE) built entirely on PySide6 and standard Python libraries. Designed for absolute privacy and local-first execution, PipViper decouples heavy processes (linters, completions, packages, version control, and AI) into thread-safe background workers to keep your main graphical user interface perfectly fluid.

---

## 🌟 Key Features

*   **Dual-Engine Code Repair**:
    *   **Deterministic (Code Tools)**: Rule-based, compiler-validated syntax auto-fixer that resolves missing colons, unbalanced brackets, tab/space mix-ups, unclosed string literals, and obsolete Python 2 print statements natively.
    *   **Semantic (Local AI Sidecar)**: Non-deterministic generative refactoring powered by local models (e.g., `qwen2.5-coder:7b`) to handle structural rearrangements and complex algorithm rewrites offline.
*   **Smart Workspace & `.venv` Auto-Discovery**: Automatically scans your active project directory, maps platform-specific interpreters (Unix `bin/python` vs. Windows `Scripts/python.exe`), and dynamically retargets your runs, debugpy debuggers, and REPL subshells to your project's local virtual environment.
*   **Widescreen-Optimized Bottom Dock**: All action controls inside the **Code Tools** panel are aligned horizontally to maximize horizontal screen real estate. This prevents layout compression on short screens, leaving the entire lower half open for logging grids and diagnostics.
*   **Self-Healing Environment Scanner**: Statically analyzes your open files using an Abstract Syntax Tree (AST) to detect unresolved imports, prompting you with an inline warning banner to auto-install missing packages using `uv` or falling back cleanly to standard `pip` if `uv` is absent.
*   **Jedi Workspace Indexing**: Leverages sequential background `jedi.Project` configurations, allowing Jedi's autocompletion, hover documentation, signatures, and go-to-definition engines to resolve your own local modules across files instantly.
*   **REPL Navigation History**: Implements a non-blocking keyboard event interceptor on the interactive subshell input field, allowing you to cycle through previously executed commands with the **Up** and **Down** arrow keys.

---

## 📂 Project Architecture

```text
pip-viper/
├── src/
│   ├── __init__.py         # Package entry point (non-blocking re-export bridge)
│   ├── __main__.py         # Standard python -m entry point
│   ├── app.py              # MainWindow orchestrator, slots, and signal routing
│   ├── code_tools.py       # Syntax repair, Black formatter, isort, autoflake, generators
│   ├── editor.py           # CodeEditor, state-machine highlighter, auto-indenter
│   ├── panels.py           # Bottom-dock panels (Output, REPL, CodeTools, AI, Git, Packages, Log)
│   ├── pip_viper.py        # Core models, trace logging, background workers, JediService
│   ├── styles.py           # Comprehensive global QSS flat stylesheet generator
│   └── widgets.py          # Sidebar widgets (Workspace File Explorer, Document Outline)
├── .gitignore              # Standard python/IDE build-artifact mask
├── launcher.py             # Desktop startup script (environmental locks & DPI scaling)
└── pyproject.toml          # Modern PEP 621 packaging and dependencies metadata
🚀 Installation & Setup
Prerequisites
Python: Version 3.10 or newer.
uv (Optional, highly recommended): For ultra-fast package listings and installations.
Installing in Development Mode
Clone the repository and install it in editable mode inside your virtual environment:
code
Bash
# Clone the repository
git clone https://github.com/your-username/pip-viper.git
cd pip-viper

# Create and activate your virtual environment
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install editable dependencies
uv pip install -e .
💻 Usage
Launching the IDE
You can start the desktop application either through your terminal or by executing the root launcher script:
code
Bash
# Method A: Package Execution
python -m src

# Method B: Direct Launcher
python launcher.py

# Method C: Installed CLI Trigger (after running uv pip install)
pip-viper
Local AI Sidecar Integration
To use the built-in offline AI Assistant:
Download and run Ollama locally on your machine.
Pull a coding model (such as Qwen 2.5 Coder):
code
Bash
ollama pull qwen2.5-coder:1.5b
Ensure Ollama is running, open the right-side AI Sidecar panel in PipViper, configure your model tag (e.g. qwen2.5-coder:1.5b), and start querying or refactoring locally.
🧪 Local Fine-Tuning Pipeline (Unsloth QLoRA)
If you have highly optimized Python training datasets (such as a custom "Python God Coder" dataset), you can fine-tune Qwen2.5-Coder-7B-Instruct locally and export it directly to GGUF format.
1. Structure Your Dataset (dataset.jsonl)
Format your custom training examples into a standard JSONL file containing OpenAI-style chat messages matching Qwen's tokens:
code
JSON
{"messages": [{"role": "system", "content": "You are a professional Python software engineer."}, {"role": "user", "content": "Write a custom QLineEdit in PySide6."}, {"role": "assistant", "content": "```python\n..."}]}
2. Run the Unsloth Training Script
Save and execute this training script using the pre-staged unsloth environment inside your local virtualenv:
code
Python
# unsloth_train.py
import torch
from datasets import load_dataset
from unsloth import FastLanguageModel
from trl import SFTConfig, SFTTrainer

# Config
MAX_SEQ_LENGTH = 2048
MODEL_NAME = "Qwen/Qwen2.5-Coder-7B-Instruct"
DATASET_PATH = "dataset.jsonl"
GGUF_OUTPUT_DIR = "./qwen-god-coder-gguf"

# Load Model
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name=MODEL_NAME,
    max_seq_length=MAX_SEQ_LENGTH,
    dtype=None,
    load_in_4bit=True,
)

# Configure LoRA adapters
model = FastLanguageModel.get_peft_model(
    model,
    r=16,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    lora_alpha=32,
    lora_dropout=0,
    bias="none",
    use_gradient_checkpointing="unsloth",
    random_state=3407,
)

# Load and format dataset
dataset = load_dataset("json", data_files=DATASET_PATH, split="train")
formatted_dataset = dataset.map(
    lambda x: {"text": [tokenizer.apply_chat_template(conv, tokenize=False) for conv in x["messages"]]},
    batched=True
)

# Train
trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=formatted_dataset,
    dataset_text_field="text",
    max_seq_length=MAX_SEQ_LENGTH,
    args=SFTConfig(
        output_dir="./outputs",
        per_device_train_batch_size=2,
        gradient_accumulation_steps=8,
        learning_rate=2e-4,
        num_train_epochs=3,
        fp16=not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        logging_steps=1,
        dataset_text_field="text",
        report_to=[],
    )
)
trainer.train()

# Merge weights and export directly to Q8_0 GGUF
model.save_pretrained_gguf(GGUF_OUTPUT_DIR, tokenizer, quantization_method="q8_0")
print("[unsloth] Pipeline complete! GGUF file generated.")
3. Register with Ollama
Create a file named Modelfile inside ./qwen-god-coder-gguf:
code
Dockerfile
FROM ./model-q8_0.gguf
TEMPLATE """{{ if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}<|im_start|>user
{{ .Prompt }}<|im_end|>
<|im_start|>assistant
"""
PARAMETER stop "<|im_start|>"
PARAMETER stop "<|im_end|>"
Build the model locally:
code
Bash
ollama create qwen2.5-coder:7b-god -f Modelfile
Type qwen2.5-coder:7b-god into your PipViper AI Sidecar panel and enjoy fully private, customized, and accelerated AI assistance!
📜 License
This project is licensed under the MIT License. See LICENSE for details.