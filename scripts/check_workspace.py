"""Offline checks equivalent to the review's forbidden-operation grep."""
import ast
import json
from pathlib import Path
import re
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "py_modules"))
from resource_files import PINS
from safe_fs import digest


def check():
    files = [BASE / "main.py", *(BASE / "py_modules").glob("*.py")]
    for path in files:
        ast.parse(path.read_text(), filename=str(path))
    for path in [*files, BASE / "plugin.json"]:
        assert not re.search(r"_root|steamcmd_linux|subprocess|extractall|shell=True", path.read_text()), path
    plugin = json.loads((BASE / "plugin.json").read_text())
    assert plugin["name"] == "CreamDeck Hardened" and plugin["flags"] == []
    assert json.loads((BASE / "package.json").read_text())["name"] == "creamdeck-hardened"
    assert '"/creamdeck-hardened/:appid"' in (BASE / "src/index.tsx").read_text()
    assert {x.name for x in (BASE / "resources").iterdir()} == {"SmokeAPI"}
    for name in ("creamapi", "screamapi", "uplay_r1", "uplay_r2", "koaloader", "steamcmd"):
        assert not (BASE / "py_modules" / (name + ".py")).exists()
    for name, expected in PINS.items():
        assert digest((BASE / "resources/SmokeAPI" / name).read_bytes()) == expected
    print(f"PASS: {len(files)} Python files parse; forbidden-operation scan has zero matches")
    print("PASS: distinct plugin/route identity, deleted unlockers, and both resource pins")


if __name__ == "__main__":
    check()
