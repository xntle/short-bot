"""Shared pytest setup.

The main job here is making `import shorts_bot` work regardless of what the
bot script is actually called. The file gets version suffixes in normal use
("shorts_bot V1.py"), which is not an importable module name, so every test
module would fail at collection with ModuleNotFoundError. Rather than pin the
tests to one filename, find the script and register it under the plain name.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_bot():
    """Import the bot script as `shorts_bot`, whatever its filename is."""
    if "shorts_bot" in sys.modules:
        return sys.modules["shorts_bot"]

    exact = ROOT / "shorts_bot.py"
    if exact.is_file():
        candidates = [exact]
    else:
        # newest first, so "shorts_bot V2.py" wins over "shorts_bot V1.py"
        candidates = sorted(ROOT.glob("shorts_bot*.py"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError(
            f"no shorts_bot*.py found in {ROOT}. The tests need the bot script "
            f"to sit next to the tests/ folder."
        )

    spec = importlib.util.spec_from_file_location("shorts_bot", candidates[0])
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {candidates[0]}")
    mod = importlib.util.module_from_spec(spec)
    # register before exec so any self-import inside the module resolves
    sys.modules["shorts_bot"] = mod
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(ROOT))   # lets `import ui` work too
_load_bot()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "ffmpeg: needs a real ffmpeg/ffprobe install and encodes tiny test clips",
    )
    config.addinivalue_line(
        "markers",
        "ratelimit: exercises the real download throttle, so it runs slowly",
    )
