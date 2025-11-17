"""Initializes the modules package."""

import sys
from pathlib import Path

modules_dir: Path = Path(__file__).parent
plugin_dir: Path = modules_dir.parent.resolve()

# Add the plugin directory to the python path
if str(plugin_dir) not in sys.path:
    sys.path.append(str(plugin_dir))
