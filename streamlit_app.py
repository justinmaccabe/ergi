"""Entry point for Streamlit Community Cloud.

Streamlit Cloud runs a file from the repository root, so this is a thin shim
around the real application in `src/desk/app/main.py`.

The `sys.path` insertion below is deliberate. This project uses a src layout,
which keeps the importable package separate from the repository's supporting
files — that separation is what lets CI assert the analytics package cannot
reach configuration or the database. Streamlit Cloud installs from
`requirements.txt` but does not necessarily install the repository itself as a
package, so without this the import would fail at boot on their infrastructure
while working perfectly on a developer machine. Belt and braces: if the package
is properly installed, the path insertion is a harmless no-op.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Run the app as __main__ so its module-level main() call fires exactly once,
# rather than importing it and relying on import side effects.
runpy.run_path(str(SRC / "desk" / "app" / "main.py"), run_name="__main__")
