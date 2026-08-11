import sys
from pathlib import Path

# Gets the root directory (multiACE-1) and adds it to the Python path
root_dir = str(Path(__file__).resolve().parents[1])
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)