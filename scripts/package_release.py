"""Create and verify the reviewable Decky package without a network or VCS writes."""
from pathlib import Path
import hashlib
import json
import re
import sys
import zipfile

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "py_modules"))
from resource_files import PINS


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sources():
    files = [BASE / name for name in ("plugin.json", "package.json", "main.py", "LICENSE", "README.md")]
    files += sorted((BASE / "py_modules").glob("*.py"))
    files += sorted((BASE / "resources" / "SmokeAPI").iterdir())
    files += sorted((BASE / "dist").glob("*.js"))
    files += sorted((BASE / "dist").glob("*.js.map"))
    if not (BASE / "dist" / "index.js").is_file():
        raise ValueError("Build dist/index.js first")
    if json.loads((BASE / "plugin.json").read_text())["flags"]:
        raise ValueError("Plugin flags must be empty")
    for name, expected in PINS.items():
        if sha((BASE / "resources" / "SmokeAPI" / name).read_bytes()) != expected:
            raise ValueError(f"Resource hash mismatch: {name}")
    for path in [BASE / "main.py", BASE / "plugin.json", *(BASE / "py_modules").glob("*.py")]:
        if re.search(r"_root|steamcmd_linux|subprocess|extractall|shell=True", path.read_text()):
            raise ValueError(f"Forbidden backend operation in {path.name}")
    if {p.name for p in (BASE / "resources").iterdir()} != {"SmokeAPI"}:
        raise ValueError("Only SmokeAPI resources may remain")
    return files


def build():
    files = sources()
    out = BASE / "out"
    out.mkdir(exist_ok=True)
    archive = out / "CreamDeckHardened.zip"
    entries = {}
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in files:
            if path.is_symlink() or not path.is_file():
                raise ValueError("Package source must be a regular file")
            name = "CreamDeckHardened/" + path.relative_to(BASE).as_posix()
            data = path.read_bytes()
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 28, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, data)
            entries[name] = sha(data)
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        assert set(bundle.namelist()) == set(entries)
        for name in bundle.namelist():
            assert sha(bundle.read(name)) == entries[name]
            assert ".." not in Path(name).parts and not name.startswith("/")
    lines = [f"{sha(archive.read_bytes())}  {archive.name}"]
    lines += [f"{value}  {name}" for name, value in sorted(entries.items())]
    (out / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"Verified {archive.name}: {len(entries)} files, {archive.stat().st_size} bytes")
    print(lines[0])


if __name__ == "__main__":
    build()
