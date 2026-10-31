"""Schema-validated per-app journals, atomically replaced under the backend lock."""
import json
import functools
import os
import re
import threading
import steam_library
from safe_fs import Directory, app_id, component

LOCK = threading.RLock()
PROXIES = ("version", "winmm", "winhttp")
APIS = ("steam_api.dll", "steam_api64.dll")
CONFIG = "SmokeAPI.config.json"
FIELDS = {"schema", "app_id", "game_dir", "mode", "proxy_name", "originals", "installed",
          "created", "stages", "phase", "original_launch_options", "set_launch_options", "enabled_dlc_ids",
          "quarantine", "launch_restore", "foreign_proxies"}
OPTIONAL_FIELDS = {"game_directory"}


def locked(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        with LOCK:
            return function(*args, **kwargs)
    return call


def launch_options(original, proxy):
    if proxy not in PROXIES or not isinstance(original, str) or len(original) > 8192:
        raise ValueError("Invalid launch options or proxy")
    if "WINEDLLOVERRIDES" in original or (original not in ("", "%command%") and not re.fullmatch(r"[A-Za-z0-9_./~-]+ %command%", original)):
        raise ValueError("Complex launch options. Set the Wine override by hand before using proxy mode")
    return f'WINEDLLOVERRIDES="{proxy}=n,b" {original or "%command%"}'


def validate(record, value):
    app_id(value)
    if isinstance(record, dict) and set(record) - OPTIONAL_FIELDS in (FIELDS - {"quarantine", "launch_restore", "foreign_proxies"},
                                                   FIELDS - {"quarantine", "launch_restore"}):
        record = {**record, "quarantine": {}, "launch_restore": (
            "pending" if record["mode"] == "proxy" and record["phase"] in ("removing", "removed")
            else "acknowledged")}
    if isinstance(record, dict) and set(record) - OPTIONAL_FIELDS == FIELDS - {"foreign_proxies"}:
        record = {**record, "foreign_proxies": {}}
    if not isinstance(record, dict) or set(record) - OPTIONAL_FIELDS != FIELDS:
        raise ValueError("Malformed install record. Refusing mutation")
    if (type(record["schema"]) is not int or record["schema"] != 1 or record["app_id"] != value
            or record["mode"] not in ("direct", "proxy")
            or record["proxy_name"] not in PROXIES
            or record["phase"] not in ("installing", "installed", "removing", "removed")):
        raise ValueError("Invalid install record")
    for key in ("game_dir", "game_directory"):
        if key not in record:
            continue
        path = record[key]
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
            raise ValueError("Invalid recorded game directory")
    if ("game_directory" in record
            and os.path.commonpath([record["game_directory"], record["game_dir"]]) != record["game_directory"]):
        raise ValueError("Recorded API directory is outside the game directory")
    for key in ("originals", "installed"):
        hashes = record[key]
        if not isinstance(hashes, dict) or not hashes:
            raise ValueError("Missing recorded hashes")
        for name, sha in hashes.items():
            component(name)
            if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha):
                raise ValueError("Invalid recorded hash")
    if not set(record["originals"]).issubset(APIS):
        raise ValueError("Invalid original DLL names")
    expected = ({*record["originals"], CONFIG} if record["mode"] == "direct"
                else {record["proxy_name"] + ".dll", CONFIG})
    if set(record["installed"]) != expected:
        raise ValueError("Invalid installed file names")
    backups = {x.replace(".dll", "_o.dll") for x in record["originals"]}
    if not isinstance(record["created"], list) or any(not isinstance(x, str) for x in record["created"]):
        raise ValueError("Invalid created file list")
    if len(set(record["created"])) != len(record["created"]):
        raise ValueError("Duplicate created file names")
    if set(record["created"]) != (backups | {CONFIG} if record["mode"] == "direct" else expected):
        raise ValueError("Unexpected created file names")
    if not isinstance(record["stages"], dict) or set(record["stages"]) != expected:
        raise ValueError("Invalid staging journal")
    if (any(not isinstance(x, str) or not re.fullmatch(r"\.cdh-[0-9a-f]{32}", x)
            for x in record["stages"].values())
            or len(set(record["stages"].values())) != len(expected)):
        raise ValueError("Unsafe staging name")
    original = record["original_launch_options"]
    if not isinstance(original, str) or len(original) > 8192:
        raise ValueError("Invalid original launch options")
    try:
        desired = launch_options(original, record["proxy_name"]) if record["mode"] == "proxy" else None
    except ValueError:
        desired = None
    if record["set_launch_options"] != desired:
        raise ValueError("Invalid recorded launch change")
    if not isinstance(record["enabled_dlc_ids"], list) or len(record["enabled_dlc_ids"]) > 1000:
        raise ValueError("Invalid DLC list")
    for dlc in record["enabled_dlc_ids"]:
        app_id(dlc)
    if record["launch_restore"] not in ("pending", "acknowledged", "manual"):
        raise ValueError("Invalid launch restoration state")
    if record["mode"] == "direct" and record["launch_restore"] == "pending":
        record = {**record, "launch_restore": "acknowledged"}
    foreign = record["foreign_proxies"]
    if not isinstance(foreign, dict) or (foreign and record["mode"] != "direct"):
        raise ValueError("Invalid foreign proxy journal")
    for name, sha in foreign.items():
        component(name)
        if (name.casefold() not in {x + ".dll" for x in PROXIES}
                or not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha)):
            raise ValueError("Invalid foreign proxy hash or name")
    allowed = set()
    for name, sha in record["installed"].items():
        stage = record["stages"][name]
        allowed.update(((stage, name, sha), (stage, None, sha), (name, None, sha)))
        if record["mode"] == "direct" and name in record["originals"]:
            backup = name.replace(".dll", "_o.dll")
            original = record["originals"][name]
            allowed.update(((name, backup, original), (backup, name, original), (name, stage, sha)))
    quarantine = record["quarantine"]
    if (not isinstance(quarantine, dict) or len(quarantine) > 100
            or (quarantine and record["phase"] == "removed")):
        raise ValueError("Invalid quarantine journal")
    for name, entry in quarantine.items():
        if (not isinstance(name, str) or not re.fullmatch(r"\.cdh-q-[0-9a-f]{32}", name)
                or not isinstance(entry, dict)
                or set(entry) not in ({"source", "target", "expected", "warning"},
                                     {"source", "target", "expected", "warning", "dev", "ino"})
                or not all(isinstance(entry[k], str) for k in ("source", "expected"))
                or (entry["target"] is not None and not isinstance(entry["target"], str))
                or not isinstance(entry["warning"], str) or len(entry["warning"]) > 1024
                or (entry["source"], entry["target"], entry["expected"]) not in allowed):
            raise ValueError("Unsafe quarantine journal")
        if "dev" in entry and any(type(entry[k]) is not int or entry[k] < 0 for k in ("dev", "ino")):
            raise ValueError("Invalid quarantine directory identity")
    return record


class Store:
    def __init__(self, path):
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError("Settings directory must be absolute")
        with Directory(os.path.dirname(path)) as parent:
            parent.ensure_child_dir(os.path.basename(path))
        self.path = path

    @locked
    def load(self, value):
        value = app_id(value)
        with Directory(self.path) as directory:
            name = value + ".json"
            if not directory.exists(name):
                return None
            original = json.loads(directory.read(name, 128 * 1024))
        record = validate(original, value)
        if record["phase"] != "removed" and "game_directory" not in record:
            try:
                game = steam_library.find_game(value, unique=True)
                root = game["game_directory"] if steam_library.inside_game(game, record) else None
            except (ValueError, OSError):
                root = None
            if root is not None:
                # Preserve legacy fields exactly; validation may supply defaults in memory.
                try:
                    self._replace({**original, "game_directory": root})
                except (OSError, ValueError):
                    # Optional migration cannot prevent access to a valid legacy record.
                    pass
                else:
                    record = {**record, "game_directory": root}
        return record

    @locked
    def all(self):
        with Directory(self.path) as directory:
            names = directory.names()
        result = []
        for name in names:
            if name.endswith(".json") and name != "allowlist.json":
                result.append(self.load(app_id(name[:-5])))
            elif name.startswith(".cdh-"):
                raise ValueError("Interrupted settings write. Inspect settings before mutation")
        return result

    @locked
    def save(self, record):
        record = validate(record, record["app_id"])
        self._replace(record)

    @locked
    def _replace(self, record):
        validate(record, record["app_id"])
        with Directory(self.path) as directory:
            directory.atomic_json(record["app_id"] + ".json", json.dumps(record, sort_keys=True).encode())

    @locked
    def allowlist(self):
        with Directory(self.path) as directory:
            if not directory.exists("allowlist.json"):
                return []
            data = json.loads(directory.read("allowlist.json", 128 * 1024))
        if not isinstance(data, list) or len(data) > 1000:
            raise ValueError("Malformed owner allowlist")
        return [app_id(x) for x in data]

    def allow(self, value, confirmed):
        app_id(value)
        if confirmed is not True:
            raise ValueError("Confirm offline single-player with no anti-cheat")
        with LOCK:
            self.load(value)
            values = sorted(set(self.allowlist()) | {value})
            with Directory(self.path) as directory:
                directory.atomic_json("allowlist.json", json.dumps(values).encode())
