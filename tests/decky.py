"""No-runtime Decky stand-in. Import has no filesystem side effects."""
import logging
from pathlib import Path

DECKY_USER_HOME = str(Path(__file__).resolve().parents[1])
DECKY_PLUGIN_SETTINGS_DIR = DECKY_USER_HOME
logger = logging.getLogger("creamdeck-hardened-tests")
