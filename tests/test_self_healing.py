"""Tests for Phase 8: Self-Healing Import Resolver and Banner Integration.

Covers:
    - Extended PyPI module mapping (PIL -> pillow, yaml -> pyyaml, etc.)
    - get_missing_imports with virtualenv site-packages resolution
    - SelfHealingBanner warning display, close action, and 1-click install signal
    - EditorTabs show/hide missing imports forwarding
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src import (
    CodeEditor,
    ColorPalette,
    EditorContainer,
    EditorTabs,
    EditorTheme,
    JediService,
    get_missing_imports,
    get_palette,
    map_module_to_pypi,
)
from src.editor import SelfHealingBanner


@pytest.fixture
def palette() -> ColorPalette:
    return get_palette(EditorTheme.DARK)


@pytest.fixture
def jedi_service() -> JediService:
    return JediService()


def test_extended_map_module_to_pypi() -> None:
    assert map_module_to_pypi("PIL") == "pillow"
    assert map_module_to_pypi("cv2") == "opencv-python"
    assert map_module_to_pypi("yaml") == "pyyaml"
    assert map_module_to_pypi("serial") == "pyserial"
    assert map_module_to_pypi("fitz") == "PyMuPDF"
    assert map_module_to_pypi("OpenSSL") == "pyOpenSSL"
    assert map_module_to_pypi("docx") == "python-docx"
    assert map_module_to_pypi("arbitrary_unknown_pkg") == "arbitrary_unknown_pkg"


def test_get_missing_imports_with_custom_site_packages(tmp_path: Path) -> None:
    # Create mock virtualenv site-packages with an installed package 'custom_tool'
    site_pkgs = tmp_path / "Lib" / "site-packages"
    site_pkgs.mkdir(parents=True)
    (site_pkgs / "custom_tool.py").write_text("def run(): pass\n", encoding="utf-8")

    code = (
        "import os\n"
        "import custom_tool\n"
        "import nonexistent_lib_999\n"
    )

    # Without custom site-packages: custom_tool is flagged as missing
    missing_default = get_missing_imports(code)
    assert "nonexistent_lib_999" in missing_default
    assert "custom_tool" in missing_default

    # With custom site-packages pointing to mock environment: custom_tool is recognized as installed!
    missing_with_env = get_missing_imports(code, site_packages_paths=[str(site_pkgs)])
    assert "nonexistent_lib_999" in missing_with_env
    assert "custom_tool" not in missing_with_env


def test_self_healing_banner_actions(qtbot: Any, palette: ColorPalette) -> None:
    banner = SelfHealingBanner(palette)
    getattr(qtbot, "addWidget")(banner)

    # Initially hidden
    assert banner.isHidden()

    # Show warning with missing modules
    banner.show_warnings(["pandas", "requests"])
    assert banner.isVisible()
    assert "pandas" in banner._label.text()
    assert "requests" in banner._label.text()

    # Test install button emits install_requested
    with getattr(qtbot, "waitSignal")(banner.install_requested, timeout=1000) as blocker:
        banner._install_button.click()

    assert blocker.args[0] == ["pandas", "requests"]
    assert banner.isHidden()  # Auto-hides after clicking install

    # Test close button hides banner
    banner.show_warnings(["scipy"])
    assert banner.isVisible()
    banner._close_button.click()
    assert banner.isHidden()


def test_editor_tabs_missing_imports_integration(
    qtbot: Any, palette: ColorPalette, jedi_service: JediService, tmp_path: Path
) -> None:
    tabs = EditorTabs(palette, jedi_service)
    getattr(qtbot, "addWidget")(tabs)
    tabs.show()

    test_file = tmp_path / "script.py"
    test_file.write_text("import missing_pkg\n", encoding="utf-8")

    editor = tabs.add_editor(test_file, "import missing_pkg\n")
    container = tabs.container_for(editor)
    assert container is not None
    assert container.banner.isHidden()

    # Test showing missing imports via EditorTabs
    tabs.show_missing_imports_for_path(test_file, ["missing_pkg"])
    assert not container.banner.isHidden()
    assert "missing_pkg" in container.banner._label.text()

    # Test signal forwarding from banner to tabs
    with getattr(qtbot, "waitSignal")(tabs.install_requested, timeout=1000) as blocker:
        container.banner._on_install()

    assert blocker.args[0] == ["missing_pkg"]

    # Test hiding missing imports via EditorTabs
    tabs.show_missing_imports_for_path(test_file, ["missing_pkg"])
    assert not container.banner.isHidden()
    tabs.hide_missing_imports_for_path(test_file)
    assert container.banner.isHidden()

