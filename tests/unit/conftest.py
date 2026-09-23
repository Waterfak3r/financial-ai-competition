import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "backend" / "src"
SRC_TEXT = str(SRC)
if SRC_TEXT not in sys.path:
    sys.path.insert(0, SRC_TEXT)
