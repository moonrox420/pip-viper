#!/usr/bin/env python3
"""Launcher script for PipViper IDE.

Configures critical Qt environment parameters and ensures directory search paths 
are correctly mapped before starting the GUI application lifecycle.
"""

import os
import sys
from pathlib import Path
from src.app import main

# 1. Force the workspace root onto sys.path to guarantee imports resolve cleanly
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

# 2. Suppress noisy standard library pydevd terminal output file validation warnings
os.environ.setdefault("PYDEVD_DISABLE_FILE_VALIDATION", "1")

# 3. Configure Qt system automatic screen scale policy (prevents DPI layout blows)
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

if __name__ == "__main__":
    sys.exit(main())