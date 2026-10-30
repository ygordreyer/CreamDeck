"""One-game SmokeAPI transactions with a durable settings journal."""
from __future__ import annotations

import fnmatch
import json
import os
import stat
import uuid

from safe_fs import Directory, app_id, digest
from install_state import APIS, CONFIG, LOCK, PROXIES, launch_options
from resource_files import verified_resources
from steam_library import assert_game_idle

CHANGED = "game changed since install, run Verify integrity in Steam"
SCAN_DEPTH = 16


def anti_cheat(directory):
    def scan(folder, depth):
        for name, info in folder.entries():
            lower = name.casefold()
            if (lower in {"easyanticheat", "easyanticheat_x64.dll", "easyanticheat_eos_setup.exe",
                          "battleye", "beclient_x64.dll", "start_protected_game.exe"}
                    or fnmatch.fnmatchcase(lower, "beservice*.exe")):
                raise ValueError(f"Anti-cheat marker found: {name}")
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"Symlink in game scan: {name}")
            if stat.S_ISDIR(info.st_mode):
                if not depth:
                    raise ValueError(f"Anti-cheat scan incomplete: folders beyond the {SCAN_DEPTH}-folder limit were not checked")
                with folder.child(name) as child:
                    scan(child, depth - 1)
    scan(directory, SCAN_DEPTH)


def api_directory(root, depth=SCAN_DEPTH):
    """Top level if it has a Steam API DLL, else the one nested folder that does (Unreal keeps it under Engine/Binaries/ThirdParty)."""
    found, variants, native, truncated = [], [], [], []

    def scan(folder, path, left):
        entries = folder.entries()
        if any(name in APIS and stat.S_ISREG(info.st_mode) for name, info in entries):
            found.append(path)
        variants.extend(name for name, _ in entries if name.casefold() in APIS and name not in APIS)
        native.extend(name for name, _ in entries if name == "libsteam_api.so")
        for name, info in entries:
            if stat.S_ISDIR(info.st_mode):
                if not left:
                    truncated.append(name)
                    continue
                with folder.child(name) as child:
                    scan(child, os.path.join(path, name), left - 1)
    with Directory(root) as directory:
        if any(directory.exists(name) for name in APIS):
            return root
        scan(directory, root, depth)
    if not found:
        if variants:
            raise ValueError(f"Steam API DLL with unexpected letter case: {variants[0]}. Not supported yet")
        if native:
            raise ValueError("This is the native Linux build. Use the Proton game build")
        if truncated:
            raise ValueError(f"No Steam API DLL within {depth} folders of the game folder. Deeper folders were not searched")
        raise ValueError("No supported Windows Steam API DLL found in the game folder")
    if len(found) > 1:
        raise ValueError("Several Steam API DLL folders found. Refusing to pick one")
    return found[0]


def available_proxies(root):
    try:
        path = api_directory(root)
        if path != root:
            return []
        with Directory(path) as directory:
            present = {name.casefold() for name, _ in directory.entries()}
        return [name for name in PROXIES if name + ".dll" not in present]
    except (ValueError, OSError):
        # Availability is advisory. Install repeats resolution and all safety checks.
        return []


def inside_game(game, record):
    root = game["game_directory"]
    return os.path.commonpath([root, record["game_dir"]]) == root


class Installer:
    def __init__(self, store, find_game):
        self.store = store
        self.find_game = find_game

    def install(self, value, mode, proxy, enabled, original_launch, manual_launch=False):
        with LOCK:
            value = app_id(value)
            records = self.store.all()
            if any(r["app_id"] == value and r["mode"] == "proxy" and r["launch_restore"] == "pending" for r in records):
                raise ValueError("Launch options restoration is pending. Restore them or explicitly resolve by hand first")
            if any(r["phase"] != "removed" for r in records):
                raise ValueError("Remove or recover the recorded installation first. Only one game at a time")
            if value not in self.store.allowlist():
                raise ValueError("Game is not on the owner allowlist")
            if mode not in ("direct", "proxy") or proxy not in PROXIES:
                raise ValueError("Invalid SmokeAPI mode or proxy")
            if not isinstance(enabled, list) or len(enabled) > 1000:
                raise ValueError("Invalid DLC list")
            enabled = sorted({app_id(x) for x in enabled})
            if not isinstance(original_launch, str) or len(original_launch) > 8192:
                raise ValueError("Invalid original launch options")
            try:
                desired = launch_options(original_launch, proxy) if mode == "proxy" else None
            except ValueError:
                if manual_launch is not True:
                    raise
                desired = None
            game = self.find_game(value)
            assert_game_idle(game)
            resources = verified_resources()
            with Directory(game["game_directory"]) as root:
                anti_cheat(root)
            path = api_directory(game["game_directory"])
            if mode == "proxy" and path != game["game_directory"]:
                raise ValueError("Proxy mode needs the Steam API DLL in the game's top folder. Use direct mode")
            with Directory(path) as directory:
                originals = {name: directory.hash(name) for name in APIS if directory.exists(name)}
                if not originals:
                    raise ValueError("No supported Windows Steam API DLL found in the game folder")
                # Wine resolves DLL names case-insensitively, so any letter-case alias is a collision.
                present = {name.casefold(): name for name, _ in directory.entries()}
                for name in (*[x.replace(".dll", "_o.dll") for x in APIS], CONFIG,
                             *([proxy + ".dll"] if mode == "proxy" else []), ".dlc_unlocker_proxy.json"):
                    if name.casefold() in present:
                        raise ValueError(f"Foreign file already exists: {present[name.casefold()]}")
                foreign_proxies = ({name: directory.hash(name) for name, _ in directory.entries()
                                    if name.casefold() in {x + ".dll" for x in PROXIES}}
                                   if mode == "direct" else {})
                directory.rename_preflight()
                config = {"$version": 4, "logging": False, "log_steam_http": False,
                          "default_app_status": "original", "override_app_status": {},
                          "override_dlc_status": {x: "unlocked" for x in enabled},
                          "auto_inject_inventory": False, "extra_inventory_items": [], "extra_dlcs": {}}
                payload = {CONFIG: json.dumps(config, indent=2).encode()}
                if mode == "direct":
                    payload.update({name: resources[name] for name in originals})
                    created = [x.replace(".dll", "_o.dll") for x in originals] + [CONFIG]
                else:
                    if len(originals) != 1:
                        raise ValueError("Ambiguous proxy architecture. Use direct mode")
                    payload[proxy + ".dll"] = resources[next(iter(originals))]
                    created = list(payload)
                record = {"schema": 1, "app_id": value, "game_dir": path,
                          "mode": mode, "proxy_name": proxy, "originals": originals,
                          "installed": {name: digest(data) for name, data in payload.items()},
                          "created": created, "stages": {name: ".cdh-" + uuid.uuid4().hex for name in payload},
                          "phase": "installing", "original_launch_options": original_launch,
                          "set_launch_options": desired, "enabled_dlc_ids": enabled,
                          "quarantine": {}, "launch_restore": "acknowledged",
                          "foreign_proxies": foreign_proxies}
                self.store.save(record)
                directory.bind_journal(record, self.store.save)
                try:
                    for name, data in payload.items():
                        directory.write_new(record["stages"][name], data)
                    if any(directory.hash(name) != sha for name, sha in originals.items()):
                        raise ValueError("Original DLL changed while staging")
                    assert_game_idle(self.find_game(value), ignored_fds=(getattr(directory, "fd", None),))
                    if mode == "direct":
                        for name, sha in originals.items():
                            directory.rename(name, name.replace(".dll", "_o.dll"), sha)
                    for name, sha in record["installed"].items():
                        directory.rename(record["stages"][name], name, sha)
                    record["phase"] = "installed"
                    self.store.save(record)
                except BaseException:
                    self._remove(record, directory, rollback=True)
                    raise
                return record

    def status(self, value):
        with LOCK:
            self.store.all()
            record = self.store.load(app_id(value))
            if not record or record["phase"] == "removed":
                return "not installed", record
            game = self.find_game(value)
            if not inside_game(game, record):
                return "game directory changed", record
            with Directory(record["game_dir"]) as directory:
                if record["phase"] != "installed":
                    return "interrupted operation; use Remove / recover", record
                for name, sha in record["installed"].items():
                    if directory.hash(name) != sha:
                        return CHANGED, record
                for name, sha in record["originals"].items():
                    check = name.replace(".dll", "_o.dll") if record["mode"] == "direct" else name
                    if directory.hash(check) != sha:
                        return CHANGED, record
            return "installed", record

    def uninstall(self, value):
        with LOCK:
            self.store.all()
            record = self.store.load(app_id(value))
            if not record:
                raise ValueError("No valid install record. No files were removed")
            if record["phase"] == "removed":
                return record, []
            game = self.find_game(value)
            if not inside_game(game, record):
                raise ValueError("Manifest game directory changed. Inspect the record manually")
            assert_game_idle(game)
            with Directory(record["game_dir"]) as directory:
                warnings = self._remove(record, directory)
            return record, warnings

    def resolve_launch(self, value, manual=False):
        with LOCK:
            self.store.all()
            record = self.store.load(app_id(value))
            if not record or record["phase"] not in ("removing", "removed"):
                raise ValueError("Remove game files before resolving launch restoration")
            if type(manual) is not bool:
                raise ValueError("Explicit manual resolution choice required")
            record["launch_restore"] = "manual" if manual else "acknowledged"
            self.store.save(record)
            return record

    def _remove(self, record, directory, rollback=False):
        # Rollback also rechecks for writers. Only our own directory handle
        # is exempt; its presence is inherent to descriptor-relative cleanup.
        game = self.find_game(record["app_id"])
        if not inside_game(game, record):
            raise ValueError("Manifest game directory changed. Inspect the record manually")
        assert_game_idle(game, ignored_fds=(getattr(directory, "fd", None),))
        directory.bind_journal(record, self.store.save)
        record["launch_restore"] = "pending" if record["mode"] == "proxy" else "acknowledged"
        self.store.save(record)
        warnings = directory.recover_quarantine()
        advisories = []
        for name, sha in record["foreign_proxies"].items():
            try:
                unchanged = directory.hash(name) == sha
            except (ValueError, OSError):
                unchanged = False
            if not unchanged:
                advisories.append(f"Foreign proxy changed since install ({name}); left alone")
        # Check remaining names before starting fresh mutations.
        names = set(record["installed"]) | set(record["created"]) | set(record["stages"].values())
        hashes = {name: directory.hash(name) for name in names}
        previous = record["phase"]
        record["phase"] = "removing"
        self.store.save(record)
        def mutate(function, *args):
            try:
                function(*args)
                return True
            except (ValueError, FileNotFoundError) as error:
                warnings.append(str(error))
                return False
        if record["mode"] == "direct":
            for name, original in record["originals"].items():
                backup = name.replace(".dll", "_o.dll")
                installed = record["installed"][name]
                stage = record["stages"][name]
                current, saved = hashes[name], hashes[backup]
                if saved == original and current == installed and hashes[stage] is None:
                    if mutate(directory.rename, name, stage, installed):
                        if mutate(directory.rename, backup, name, original):
                            mutate(directory.delete, stage, installed)
                elif saved == original and current is None and (
                        previous == "installing" or hashes[stage] == installed):
                    # A staged installed DLL proves an interrupted removal.
                    mutate(directory.rename, backup, name, original)
                elif saved is None and current == original:
                    pass
                else:
                    warnings.append(CHANGED + f" ({name}); DLL and backup left alone")
        for name, installed in record["installed"].items():
            if record["mode"] == "direct" and name in record["originals"]:
                continue
            current = hashes[name]
            if current == installed:
                mutate(directory.delete, name, installed)
            elif current is not None:
                warnings.append(CHANGED + f" ({name}); file left alone")
        for name, stage in record["stages"].items():
            current = directory.hash(stage)
            if current == record["installed"][name]:
                mutate(directory.delete, stage, current)
            elif current is not None:
                warnings.append(f"Unrecognized staging file left alone: {stage}")
        record["phase"] = "removing" if warnings or record["quarantine"] else "removed"
        try:
            self.store.save(record)
        except OSError:
            if not rollback:
                raise
        if rollback and warnings:
            raise RuntimeError("Install rollback needs recovery: " + "; ".join(warnings))
        return warnings + advisories
