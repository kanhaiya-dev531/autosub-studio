"""Start the desktop app on your own computer:  python run_desktop.py"""

import sys

try:
    import tkinter  # noqa: F401  (only checking that it is installed)
except ImportError:
    sys.exit(
        "Tkinter is not installed for this Python.\n"
        "  Windows: re-run the Python installer, choose Modify, and tick 'tcl/tk and IDLE'.\n"
        "  Ubuntu/Debian: sudo apt install python3-tk\n"
        "  macOS (Homebrew): brew install python-tk"
    )

from desktop.app import main

if __name__ == "__main__":
    sys.exit(main())
