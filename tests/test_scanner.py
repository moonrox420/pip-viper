"""Unit tests for the self-healing AST scanner, PyPI module mapping, and theme stylesheets."""

from __future__ import annotations

from pathlib import Path

from src import (
    EditorTheme,
    get_missing_imports,
    get_palette,
    map_module_to_pypi,
)
from src.app import extract_code_from_markdown
from src.styles import get_stylesheet


def test_map_module_to_pypi_known_mappings() -> None:
    """Verify known module to PyPI package mappings."""
    assert map_module_to_pypi("yaml") == "pyyaml"
    assert map_module_to_pypi("bs4") == "beautifulsoup4"
    assert map_module_to_pypi("PIL") == "pillow"
    assert map_module_to_pypi("cv2") == "opencv-python"
    assert map_module_to_pypi("dotenv") == "python-dotenv"


def test_map_module_to_pypi_unmapped() -> None:
    """Verify unmapped modules pass through unchanged."""
    assert map_module_to_pypi("custom_package") == "custom_package"


def test_get_missing_imports_stdlib_ignored() -> None:
    """Standard library imports should never be flagged as missing."""
    source = "import os\nimport sys\nfrom pathlib import Path\nimport json\n"
    missing = get_missing_imports(source)
    assert missing == []


def test_get_missing_imports_detects_nonexistent_package() -> None:
    """Non-installed third-party imports should be detected."""
    source = "import nonexistent_fake_package_xyz123\n"
    missing = get_missing_imports(source)
    assert "nonexistent_fake_package_xyz123" in missing


def test_get_missing_imports_handles_syntax_error_gracefully() -> None:
    """Broken syntax should not crash the scanner."""
    source = "import broken(\n"
    missing = get_missing_imports(source)
    assert missing == []


def test_get_missing_imports_local_file_ignored(tmp_path: Path) -> None:
    """Local sibling .py files should not be flagged as missing."""
    local_module = tmp_path / "my_helper.py"
    local_module.write_text("def help(): pass\n", encoding="utf-8")

    source = "import my_helper\n"
    missing = get_missing_imports(source, active_file_directory=tmp_path)
    assert "my_helper" not in missing


def test_get_missing_imports_local_directory_ignored(tmp_path: Path) -> None:
    """Local sibling subpackages should not be flagged as missing."""
    subpkg = tmp_path / "my_pkg"
    subpkg.mkdir()

    source = "import my_pkg\n"
    missing = get_missing_imports(source, active_file_directory=tmp_path)
    assert "my_pkg" not in missing


def test_get_stylesheet_all_themes() -> None:
    """All built-in themes should generate non-empty, valid stylesheets."""
    for theme in EditorTheme:
        palette = get_palette(theme)
        qss = get_stylesheet(palette)
        assert isinstance(qss, str)
        assert palette.background in qss
        assert palette.text in qss


def test_extract_code_from_markdown_explicit_python_block() -> None:
    """Extract python source from ```python ... ``` block."""
    markdown = "Here is the refactored code:\n```python\ndef foo():\n    return 42\n```\nHope this helps!"
    extracted = extract_code_from_markdown(markdown)
    assert extracted == "def foo():\n    return 42"


def test_extract_code_from_markdown_generic_block() -> None:
    """Extract code from generic ``` ... ``` block."""
    markdown = "```\nx = 1\n```"
    extracted = extract_code_from_markdown(markdown)
    assert extracted == "x = 1"


def test_extract_code_from_markdown_plain_code() -> None:
    """Plain code with no markdown fences remains intact."""
    plain = "def foo():\n    pass"
    extracted = extract_code_from_markdown(plain)
    assert extracted == plain
