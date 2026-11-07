from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_PKG_ROOT = os.path.join(REPO_ROOT, "plugin", "kitbridge")
if PLUGIN_PKG_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_PKG_ROOT)
