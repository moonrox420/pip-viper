"""
src launcher script (top-level, Windows-friendly).
"""

import sys
from pathlib import Path

# Ensure the package can be imported when running directly
project_root = Path(__file__).parent.resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Import and run
try:
    from src import main
except ImportError as e:
    print("ERROR: Could not import src package.", file=sys.stderr)
    print(
        "Make sure you are in the project root and the package is installed or discoverable.",
        file=sys.stderr,
    )
    print(f"Details: {e}", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    sys.exit(main())
