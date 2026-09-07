"""
Python Project Import and Dependency Analyzer (2026 Production-Grade)
======================================================================
Analyzes Python source files in a directory to identify:
1. Missing third-party dependencies (imported but not installed).
2. Broken local imports (references to files/modules that do not exist).
3. Successfully installed third-party dependencies (with version detection).
4. Standard library imports.
5. Resolved internal project submodules.

Supports absolute and relative imports, handles syntax errors gracefully,
and can export to both JSON and requirements.txt formats.
"""

import argparse
import ast
import importlib.metadata
import importlib.util
import json
import os
import sys
from pathlib import Path

# Force UTF-8 encoding on Windows to prevent UnicodeEncodeError with emoji glyphs
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# --- ANSI Terminal Formatting ---
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def supports_color() -> bool:
    """Detects if the terminal session supports colored output."""
    if not sys.stdout.isatty():
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


if not supports_color():
    GREEN = RED = YELLOW = BLUE = CYAN = BOLD = RESET = ""

# --- Standard Library Recognition (Resilient Setup) ---
try:
    STDLIB_MODULES = sys.stdlib_module_names | set(sys.builtin_module_names)
except AttributeError:
    # Safe fallback for legacy Python versions (< 3.10)
    STDLIB_MODULES = set(sys.builtin_module_names) | {
        "os",
        "sys",
        "pathlib",
        "json",
        "re",
        "math",
        "collections",
        "datetime",
        "typing",
        "urllib",
        "http",
        "socket",
        "subprocess",
        "shutil",
        "logging",
        "hashlib",
        "time",
        "random",
        "csv",
        "sqlite3",
        "xml",
        "uuid",
        "abc",
        "argparse",
        "ast",
        "asyncio",
        "base64",
        "bisect",
        "copy",
        "ctypes",
        "decimal",
        "difflib",
        "dis",
        "email",
        "enum",
        "fnmatch",
        "functools",
        "gc",
        "glob",
        "gzip",
        "importlib",
        "inspect",
        "io",
        "itertools",
        "locale",
        "multiprocessing",
        "operator",
        "pickle",
        "platform",
        "pprint",
        "queue",
        "sched",
        "select",
        "selectors",
        "signal",
        "ssl",
        "stat",
        "string",
        "struct",
        "tarfile",
        "tempfile",
        "threading",
        "traceback",
        "types",
        "warnings",
        "weakref",
        "zipfile",
        "zlib",
    }

# --- Distribution Mapping Cache ---
try:
    # Maps top-level import name to PyPI distribution package name (e.g. "yaml" -> ["PyYAML"])
    PKG_DISTS = importlib.metadata.packages_distributions()
except Exception:
    PKG_DISTS = {}


# --- Business Logic Implementation ---


def get_pypi_name(top_level_name: str) -> str:
    """Resolves PyPI package name from the imported top-level module name."""
    dists = PKG_DISTS.get(top_level_name)
    return dists[0] if dists else top_level_name


def get_installed_version(pypi_name: str) -> str:
    """Attempts to retrieve the installed package version from metadata."""
    try:
        return importlib.metadata.version(pypi_name)
    except Exception:
        return ""


def crawl_project_files(project_dir: Path, exclude_dirs: set[str]) -> list[Path]:
    """
    Recursively scans the directory for Python files.
    Optimized to skip scanning directories in the ignore list entirely.
    """
    py_files = []
    ignore_dirs = {
        ".git",
        ".github",
        ".venv",
        "venv",
        "env",
        ".env",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".tox",
        "build",
        "dist",
    }
    ignore_dirs.update(exclude_dirs)

    for root, dirs, files in os.walk(project_dir):
        # Modify dirs in-place to prune ignored directories from execution stack
        dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
        for file in files:
            if file.endswith(".py"):
                py_files.append(Path(root) / file)
    return py_files


def extract_imports(file_path: Path) -> list[dict]:
    """
    Parses a single file's AST and extracts all import definitions.
    Returns a list of structured dictionaries describing each import occurrence.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError as e:
        raise e
    except Exception as e:
        raise IOError(f"Could not read file: {e}")

    found_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found_imports.append(
                    {
                        "full_name": alias.name,
                        "top_level_name": alias.name.split(".")[0],
                        "line": node.lineno,
                        "is_relative": False,
                        "level": 0,
                        "names": [],
                    }
                )
        elif isinstance(node, ast.ImportFrom):
            is_relative = node.level > 0
            names = [alias.name for alias in node.names]
            found_imports.append(
                {
                    "full_name": node.module if node.module else "",
                    "top_level_name": node.module.split(".")[0] if node.module else "",
                    "line": node.lineno,
                    "is_relative": is_relative,
                    "level": node.level,
                    "names": names,
                }
            )
    return found_imports


def resolve_relative_import(
    level: int, module: str, names: list[str], importing_file: Path
) -> tuple[str, str]:
    """
    Checks if a relative import correctly points to an existing file or package.
    Returns: (status, target_path_str) where status is 'local' or 'local_missing'.
    """
    base_dir = importing_file.parent
    for _ in range(level - 1):
        base_dir = base_dir.parent

    # If base_dir moves past root, or doesn't exist, flag as broken
    if not base_dir.exists():
        return "local_missing", f"{'.' * level}{module}"

    if module:
        module_path = base_dir.joinpath(*module.split("."))
        if module_path.with_suffix(".py").is_file():
            return "local", str(module_path.with_suffix(".py"))
        if (module_path / "__init__.py").is_file():
            return "local", str(module_path / "__init__.py")
        if module_path.is_dir() and any(module_path.rglob("*.py")):
            return "local", str(module_path)
        return "local_missing", f"{'.' * level}{module}"
    else:
        # e.g. from . import sibling1, sibling2
        for name in names:
            target_path = base_dir / name
            if target_path.with_suffix(".py").is_file():
                continue
            if (target_path / "__init__.py").is_file():
                continue
            if target_path.is_dir() and any(target_path.rglob("*.py")):
                continue
            return "local_missing", f"{'.' * level} (missing: {name})"
        return "local", str(base_dir)


def resolve_absolute_import(
    top_level: str, importing_file: Path, local_roots: list[Path]
) -> str:
    """
    Classifies an absolute top-level import.
    Returns: 'standard', 'local', 'third_party_installed', or 'missing'.
    """
    if top_level in STDLIB_MODULES:
        return "standard"

    # Match against designated source roots (flat, src/, sibling folders)
    for root in local_roots:
        if (root / f"{top_level}.py").is_file():
            return "local"
        if (root / top_level / "__init__.py").is_file():
            return "local"
        # Directory without __init__.py but containing .py files (namespace package)
        m_dir = root / top_level
        if m_dir.is_dir() and any(m_dir.rglob("*.py")):
            return "local"

    # Check immediate relative sibling directory
    sibling_dir = importing_file.parent
    if (sibling_dir / f"{top_level}.py").is_file():
        return "local"
    if (sibling_dir / top_level / "__init__.py").is_file():
        return "local"

    # Attempt to locate via system / active site-packages environment
    try:
        spec = importlib.util.find_spec(top_level)
        if spec is not None:
            return "third_party_installed"
    except Exception:
        pass

    return "missing"


def format_import_statement(imp: dict) -> str:
    """Reconstructs a clean import statement string for diagnostic output."""
    if imp["is_relative"]:
        dots = "." * imp["level"]
        if imp["full_name"]:
            return f"from {dots}{imp['full_name']} import {', '.join(imp['names'])}"
        return f"from {dots} import {', '.join(imp['names'])}"
    else:
        if imp["names"]:
            return f"from {imp['full_name']} import {', '.join(imp['names'])}"
        return f"import {imp['full_name']}"


def main():
    parser = argparse.ArgumentParser(
        description="Verify import integrity and dependencies of a Python workspace."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Target project directory to analyze (default: current directory)",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        help="Subdirectories to force-exclude from crawling",
    )
    parser.add_argument(
        "--json", metavar="FILE", help="Export raw structural results to a JSON file"
    )
    parser.add_argument(
        "--requirements",
        metavar="FILE",
        help="Generate a valid requirements.txt based on discovered active imports",
    )
    args = parser.parse_args()

    project_dir = Path(args.directory).resolve()
    if not project_dir.exists():
        print(
            f"{RED}Error: Path '{project_dir}' does not exist.{RESET}", file=sys.stderr
        )
        sys.exit(1)

    print(f"{BOLD}{CYAN}======================================================{RESET}")
    print(f"{BOLD}{CYAN}     Python Dependency & Import Integrity Audit       {RESET}")
    print(f"{BOLD}{CYAN}======================================================{RESET}")
    print(f"{BOLD}Target Path:{RESET} {project_dir}")

    # Crawl files
    exclude_dirs = set(args.exclude)
    py_files = crawl_project_files(project_dir, exclude_dirs)
    print(f"{BOLD}Found:{RESET} {len(py_files)} '.py' source files to check.")

    # Determine standard resolution roots
    local_roots = [project_dir]
    for src_dir in ["src", "lib", "source"]:
        candidate = project_dir / src_dir
        if candidate.is_dir():
            local_roots.append(candidate)

    # Buckets for parsed elements
    syntax_errors = []
    scanned_count = 0

    results = {
        "missing_third_party": {},  # import name -> details
        "missing_local": {},  # import name -> details
        "installed_third_party": {},  # import name -> details
        "standard_library": {},  # import name -> details
        "local_modules": {},  # import name -> details
    }

    for file_path in py_files:
        rel_file_path = file_path.relative_to(project_dir)
        try:
            imports = extract_imports(file_path)
            scanned_count += 1
        except SyntaxError as e:
            syntax_errors.append(
                {"file": str(rel_file_path), "line": e.lineno, "error": e.msg}
            )
            continue
        except Exception as e:
            print(f"{RED}Error processing {rel_file_path}: {e}{RESET}", file=sys.stderr)
            continue

        for imp in imports:
            stmt = format_import_statement(imp)
            occurrence = {
                "file": str(rel_file_path),
                "line": imp["line"],
                "statement": stmt,
            }

            if imp["is_relative"]:
                status, target = resolve_relative_import(
                    imp["level"], imp["full_name"], imp["names"], file_path
                )
                if status == "local":
                    results["local_modules"].setdefault(target, []).append(occurrence)
                else:
                    results["missing_local"].setdefault(target, []).append(occurrence)
            else:
                top_level = imp["top_level_name"]
                if not top_level:
                    continue  # Sanity fallback

                category = resolve_absolute_import(top_level, file_path, local_roots)

                if category == "standard":
                    results["standard_library"].setdefault(top_level, []).append(
                        occurrence
                    )
                elif category == "local":
                    results["local_modules"].setdefault(top_level, []).append(
                        occurrence
                    )
                elif category == "third_party_installed":
                    pypi_name = get_pypi_name(top_level)
                    if top_level not in results["installed_third_party"]:
                        results["installed_third_party"][top_level] = {
                            "pypi_name": pypi_name,
                            "version": get_installed_version(pypi_name),
                            "occurrences": [],
                        }
                    results["installed_third_party"][top_level]["occurrences"].append(
                        occurrence
                    )
                else:
                    pypi_name = get_pypi_name(top_level)
                    if top_level not in results["missing_third_party"]:
                        results["missing_third_party"][top_level] = {
                            "pypi_name": pypi_name,
                            "occurrences": [],
                        }
                    results["missing_third_party"][top_level]["occurrences"].append(
                        occurrence
                    )

    # --- Print Reporting ---

    # 1. Syntax Failures
    if syntax_errors:
        print(
            f"\n{RED}{BOLD}⚠️  SYNTAX ERRORS DETECTED ({len(syntax_errors)} files):{RESET}"
        )
        for err in syntax_errors:
            print(f"  • {err['file']}:{err['line']} -> {RED}{err['error']}{RESET}")

    # 2. Critical Block: Missing Third-Party Imports
    print(f"\n{BOLD}------------------------------------------------------{RESET}")
    print(f"{RED}{BOLD}❌  MISSING THIRD-PARTY DEPENDENCIES{RESET}")
    print(f"{BOLD}------------------------------------------------------{RESET}")
    if results["missing_third_party"]:
        print(
            "These modules are imported but cannot be found in your active environment:"
        )
        for top_level, details in sorted(results["missing_third_party"].items()):
            pypi = details["pypi_name"]
            pkg_help = f" (Likely PyPI package: '{pypi}')" if pypi != top_level else ""
            print(f"\n  • {RED}{BOLD}{top_level}{RESET}{pkg_help}")
            for occ in details["occurrences"]:
                print(
                    f"    - {CYAN}{occ['file']}:{occ['line']}{RESET} -> `{occ['statement']}`"
                )
    else:
        print(f"{GREEN}No missing third-party packages detected.{RESET}")

    # 3. Missing Local Imports
    print(f"\n{BOLD}------------------------------------------------------{RESET}")
    print(f"{YELLOW}{BOLD}⚠️  BROKEN LOCAL OR RELATIVE IMPORTS{RESET}")
    print(f"{BOLD}------------------------------------------------------{RESET}")
    if results["missing_local"]:
        print("These relative or workspace absolute modules were not found physically:")
        for target, occurrences in sorted(results["missing_local"].items()):
            print(f"\n  • {YELLOW}{BOLD}{target}{RESET}")
            for occ in occurrences:
                print(
                    f"    - {CYAN}{occ['file']}:{occ['line']}{RESET} -> `{occ['statement']}`"
                )
    else:
        print(f"{GREEN}All local and relative imports resolved perfectly.{RESET}")

    # 4. Resolved Third-Party
    print(f"\n{BOLD}------------------------------------------------------{RESET}")
    print(f"{GREEN}{BOLD}📦  RESOLVED THIRD-PARTY DEPENDENCIES{RESET}")
    print(f"{BOLD}------------------------------------------------------{RESET}")
    if results["installed_third_party"]:
        for top_level, details in sorted(results["installed_third_party"].items()):
            ver = (
                f" v{details['version']}"
                if details["version"]
                else " (version unknown)"
            )
            print(
                f"  • {BOLD}{details['pypi_name']}{RESET}{ver} (imported as '{top_level}')"
            )
    else:
        print("No active third-party packages found.")

    # 5. Standard Library Modules
    print(f"\n{BOLD}------------------------------------------------------{RESET}")
    print(f"{BLUE}{BOLD}🐍  STANDARD LIBRARY MODULES USED{RESET}")
    print(f"{BOLD}------------------------------------------------------{RESET}")
    if results["standard_library"]:
        std_list = ", ".join(sorted(results["standard_library"].keys()))
        print(f"  {std_list}")
    else:
        print("  None detected.")

    # 6. Local Modules Verified
    print(f"\n{BOLD}------------------------------------------------------{RESET}")
    print(f"{CYAN}{BOLD}🏠  RESOLVED INTERNAL MODULES & MODULE ROOTS{RESET}")
    print(f"{BOLD}------------------------------------------------------{RESET}")
    if results["local_modules"]:
        loc_list = ", ".join(sorted(results["local_modules"].keys()))
        print(f"  {loc_list}")
    else:
        print("  No relative/local modules found.")

    # --- Write Outputs ---

    # Export to JSON
    if args.json:
        json_path = Path(args.json).resolve()
        export_payload = {
            "summary": {
                "scanned_directory": str(project_dir),
                "total_files_scanned": scanned_count,
                "syntax_failures": len(syntax_errors),
            },
            "syntax_errors": syntax_errors,
            "analysis": results,
        }
        try:
            with open(json_path, "w", encoding="utf-8") as jf:
                json.dump(export_payload, jf, indent=2)
            print(f"\n{GREEN}✔ Raw JSON analysis exported to: {json_path}{RESET}")
        except Exception as e:
            print(f"\n{RED}Error exporting JSON: {e}{RESET}", file=sys.stderr)

    # Export to requirements.txt
    if args.requirements:
        req_path = Path(args.requirements).resolve()
        lines = []
        for top_level, details in results["installed_third_party"].items():
            name = details["pypi_name"]
            ver = details["version"]
            lines.append(f"{name}=={ver}" if ver else name)
        lines.sort()

        try:
            with open(req_path, "w", encoding="utf-8") as rf:
                rf.write("# Generated automatically by imports auditing script\n")
                rf.write("\n".join(lines) + "\n")
            print(f"{GREEN}✔ Clean, pinned dependencies written to: {req_path}{RESET}")
        except Exception as e:
            print(
                f"\n{RED}Error writing requirements file: {e}{RESET}", file=sys.stderr
            )


if __name__ == "__main__":
    main()
