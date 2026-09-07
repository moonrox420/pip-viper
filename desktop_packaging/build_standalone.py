#!/usr/bin/env python3
"""Standalone desktop packaging and build validation script for PipViper IDE.

Verifies build prerequisites, performs automated headless smoke tests on all
internal packages, and orchestrates PyInstaller / wheel builds.
"""

from __future__ import annotations

import argparse
import importlib
import logging
from pathlib import Path
import subprocess
import sys

_LOGGER = logging.getLogger("desktop_packaging.build_standalone")


def validate_environment() -> tuple[bool, list[str]]:
    """Verify that Python version and critical dependencies meet minimum specs."""
    issues: list[str] = []
    if sys.version_info < (3, 10):
        issues.append(f"Python 3.10+ is required (found {sys.version.split()[0]})")

    required_modules = ["PySide6", "pydantic", "jedi"]
    for mod in required_modules:
        try:
            importlib.import_module(mod)
        except ImportError:
            issues.append(f"Required package '{mod}' is not installed in the active environment.")

    return len(issues) == 0, issues


def check_release_pollution(project_root: Path) -> list[str]:
    """Scan the project tree and release archives for forbidden release pollution (PRD P1)."""
    pollutants: list[str] = []
    forbidden_patterns = ["__pycache__", ".pytest_tmp*", "_pytest_tmp*", "temp_*", "_test_*", "*.pyc"]
    for pattern in forbidden_patterns:
        for match in project_root.glob(f"**/{pattern}"):
            # Exclude virtual environments or git internals
            parts = match.parts
            if any(p.startswith(".venv") or p.startswith(".git") for p in parts):
                continue
            pollutants.append(str(match.relative_to(project_root)))

    # Also inspect all zip archives in root
    for zip_path in project_root.glob("*.zip"):
        try:
            import zipfile
            with zipfile.ZipFile(zip_path, "r") as zf:
                for name in zf.namelist():
                    if any(bad in name for bad in ("__pycache__", ".pytest_tmp", "_pytest_tmp", "temp_")) or name.endswith(".pyc"):
                        pollutants.append(f"{zip_path.name}:{name}")
        except Exception as exc:
            pollutants.append(f"{zip_path.name}:failed_to_read ({exc})")

    return pollutants


def clean_workspace_pollution(project_root: Path) -> int:
    """Purge temporary cache and build pollution from workspace."""
    import shutil
    cleaned_count = 0
    forbidden_patterns = ["__pycache__", ".pytest_tmp*", "_pytest_tmp*", "temp_*", "_test_*", "*.pyc"]
    for pattern in forbidden_patterns:
        for match in list(project_root.glob(f"**/{pattern}")):
            parts = match.parts
            if any(p.startswith(".venv") or p.startswith(".git") for p in parts):
                continue
            try:
                if match.is_dir():
                    shutil.rmtree(match, ignore_errors=True)
                elif match.is_file():
                    match.unlink(missing_ok=True)
                cleaned_count += 1
            except Exception as exc:
                print(f"[WARN] Could not remove {match}: {exc}")
    return cleaned_count


def create_clean_release_zip(project_root: Path, zip_output: Path) -> bool:
    """Create a pristine release archive guaranteed free of bytecode and temp files (PRD P1)."""
    import zipfile
    clean_workspace_pollution(project_root)

    forbidden_names = {"__pycache__", ".git", ".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
    temp_zip = zip_output.with_suffix(".tmp.zip")

    with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in project_root.rglob("*"):
            if path.is_file():
                rel_path = path.relative_to(project_root)
                # Skip forbidden directories
                if any(part in forbidden_names for part in rel_path.parts):
                    continue
                if any(part.startswith((".pytest_tmp", "_pytest_tmp", "temp_", "_test_")) for part in rel_path.parts):
                    continue
                if path.suffix.lower() in (".pyc", ".pyo", ".pyd") or path.name.endswith(".zip"):
                    continue
                zf.write(path, str(rel_path).replace("\\", "/"))

    # Validate output zip before committing
    with zipfile.ZipFile(temp_zip, "r") as zf:
        bad = [n for n in zf.namelist() if "__pycache__" in n or n.endswith(".pyc") or "pytest" in n or "temp_" in n]
        if bad:
            print(f"[FAIL] Created zip failed pollution check: {len(bad)} forbidden files.")
            temp_zip.unlink(missing_ok=True)
            return False

    if zip_output.exists():
        zip_output.unlink()
    temp_zip.rename(zip_output)
    print(f"[OK] Clean release archive successfully built: {zip_output.name} (zero pollution).")
    return True


def smoke_test_modules() -> list[tuple[str, bool, str]]:
    """Import all PipViper internal modules headlessly to ensure zero missing symbols."""
    modules_to_test = [
        "src.pip_viper",
        "src.services",
        "src.controllers",
        "src.styles",
        "src.editor",
        "src.panels",
        "src.widgets",
        "src.internals",
        "src.profiler_visualizer",
        "src.memory_tracker",
        "src.environment",
        "src.dependencies",
        "src.vcs",
        "src.diff_viewer",
        "src.diagnostics",
        "src.testing",
        "src.repl_harness",
        "src.code_tools",
        "src.debug_harness",
        "src.navigation",
        "src.app",
    ]

    results: list[tuple[str, bool, str]] = []
    for mod_name in modules_to_test:
        try:
            importlib.import_module(mod_name)
            results.append((mod_name, True, "OK"))
        except Exception as e:
            results.append((mod_name, False, str(e)))

    return results


def verify_spec_file(spec_path: Path) -> bool:
    """Validate that the PyInstaller spec file exists and is non-empty."""
    return spec_path.is_file() and spec_path.stat().st_size > 0


def build_pyinstaller(spec_path: Path) -> int:
    """Invoke PyInstaller compiler with the project spec file."""
    cmd = [sys.executable, "-m", "PyInstaller", str(spec_path), "--noconfirm"]
    print(f"Executing: {' '.join(cmd)}")
    return subprocess.call(cmd)


def main() -> int:
    # Ensure UTF-8 output on Windows consoles if supported
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="PipViper Desktop Packaging Utility")
    parser.add_argument("--smoke-test", action="store_true", help="Run import smoke test on all modules")
    parser.add_argument("--validate", action="store_true", help="Verify build environment, hygiene, and specs")
    parser.add_argument("--clean", action="store_true", help="Purge temporary workspace and build cache files")
    parser.add_argument("--create-zip", action="store_true", help="Package pristine, clean release ZIP (PRD P1)")
    parser.add_argument("--build", action="store_true", help="Compile standalone executable using PyInstaller")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    spec_path = project_root / "desktop_packaging" / "pip_viper.spec"
    zip_path = project_root / "pipviper.zip"

    if args.clean:
        count = clean_workspace_pollution(project_root)
        print(f"[OK] Cleaned {count} temporary/cache pollution items from workspace.")

    if args.create_zip:
        success = create_clean_release_zip(project_root, zip_path)
        if not success:
            return 1

    # Default action: run validation & smoke test
    if not (args.smoke_test or args.validate or args.build or args.clean or args.create_zip):
        args.validate = True
        args.smoke_test = True

    if args.validate:
        print("=== Checking Build Environment & Hygiene ===")
        ok, issues = validate_environment()
        if not ok:
            for issue in issues:
                print(f"[FAIL] {issue}")
            return 1
        print("[OK] Environment meets all deployment prerequisites.")

        if verify_spec_file(spec_path):
            print(f"[OK] PyInstaller spec verified: {spec_path.name}")
        else:
            print(f"[FAIL] Spec file missing or empty: {spec_path}")
            return 1

        pollution = check_release_pollution(project_root)
        if pollution:
            print(f"[FAIL] Detected {len(pollution)} forbidden release pollution items (PRD P1 violated):")
            for p in pollution[:10]:
                print(f"       - {p}")
            if len(pollution) > 10:
                print(f"       ... and {len(pollution) - 10} more items.")
            return 1
        else:
            print("[OK] Release workspace hygiene verified (zero build pollution).")

    if args.smoke_test:
        print("\n=== Running Module Smoke Tests ===")
        test_results = smoke_test_modules()
        all_passed = True
        for mod_name, success, msg in test_results:
            status = "[OK]  " if success else "[FAIL]"
            print(f"  {status} {mod_name:<30} {msg}")
            if not success:
                all_passed = False

        if not all_passed:
            print("\n[FAIL] Smoke test failed: Some internal modules could not be imported.")
            return 1
        print("\n[OK] All internal modules imported cleanly with zero missing symbols.")

    if args.build:
        print("\n=== Compiling Standalone Executable ===")
        return build_pyinstaller(spec_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
