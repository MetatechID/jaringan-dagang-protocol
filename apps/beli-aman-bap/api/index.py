"""Vercel @vercel/python entry point for the Beli Aman BAP.

The Vercel project's rootDirectory is `apps/beli-aman-bap`, so this file is
at `<root>/api/index.py` from Vercel's perspective and `main.py` is at
`<root>/main.py`. The shim adds the parent dir to sys.path then imports `app`.
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_app_dir = os.path.dirname(_here)  # apps/beli-aman-bap (the Vercel root)

if _app_dir not in sys.path:
    sys.path.insert(0, _app_dir)

# Use __import__ to avoid name-collision between Python's `app` module and
# the FastAPI `app` instance Vercel expects.
_main = __import__("main", fromlist=["app"])
app = _main.app
