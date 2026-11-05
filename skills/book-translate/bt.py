# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pikepdf==10.*",
#     "pdfplumber==0.11.*",
#     "pypdfium2==4.*",
#     "pyyaml==6.*",
#     "pymorphy3==2.*",
# ]
# ///
"""Run the bt CLI from any folder: uv run <skill folder>/bt.py <command>."""

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # the skill folder stays exactly as it was installed
sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))

from bt.cli import main

if __name__ == "__main__":
    sys.exit(main())
