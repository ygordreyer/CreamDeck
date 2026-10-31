"""Per-game SmokeAPI transactions with durable settings journals."""
from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import stat
import uuid
from contextlib import ExitStack

from safe_fs import Directory, app_id, digest
from install_state import APIS, CONFIG, LOCK, PROXIES, launch_options
from resource_files import verified_resources
from steam_library import assert_game_idle, inside_game, relative_executable

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


def unity_amd64_directory(root, found, relative, executables):
    unique = {}
    for path in executables:
        unique.setdefault(path.casefold(), path)
    if len(unique) != 1 or len(found) != 2:
        return None
    executable = next(iter(unique.values()))
    folder, name = posixpath.split(executable)
    if not name.casefold().endswith(".exe"):
        return None
    plugins = posixpath.join(folder, name[:-4] + "_Data", "Plugins")
    expected = [posixpath.join(plugins, arch).casefold() for arch in ("x86", "x86_64")]
    if set(relative) != set(expected):
        return None
    selected = found[relative.index(expected[1])]
    try:
        with ExitStack() as stack:
            directory = stack.enter_context(Directory(root))
            binary = directory
            for part in folder.split("/") if folder else ():
                binary = stack.enter_context(binary.child(part))
            header = binary.read_head(name, 4096)
            if len(header) < 0x40 or header[:2] != b"MZ":
                return None
            offset = int.from_bytes(header[0x3C:0x40], "little")
            if offset + 6 > len(header) or header[offset:offset + 4] != b"PE\0\0":
                return None
            if int.from_bytes(header[offset + 4:offset + 6], "little") != 0x8664:
                return None
            target = directory
            for part in os.path.relpath(selected, root).split(os.sep):
                target = stack.enter_context(target.child(part))
            if target.exists("steam_api64.dll"):
                return selected
    except (OSError, ValueError):
        # Failed evidence checks preserve the ambiguous-folder refusal.
        pass
    return None


def api_directory(root, depth=SCAN_DEPTH, launches=()):
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
        relative = [os.path.relpath(path, root).replace(os.sep, "/").casefold() for path in found]
        if len(set(relative)) == len(relative):
            # The provider stays lazy: ordinary single-folder installs never open appinfo.
            executables = launches() if callable(launches) else launches
            executables = [path for value in executables
                           if (path := relative_executable(value)) is not None]
            folders = {posixpath.dirname(path).casefold() for path in executables}
            selected = [path for path, name in zip(found, relative) if name in folders]
            if len(selected) == 1:
                return selected[0]
            if not selected:
                unity = unity_amd64_directory(root, found, relative, executables)
                if unity is not None:
                    return unity
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


def folders_overlap(first, second):
    first, second = os.path.normcase(first), os.path.normcase(second)
    try:
        return os.path.commonpath([first, second]) in (first, second)
    except ValueError:
        return False


def recorded_names(record):
    return (set(record["originals"]) | set(record["installed"]) | set(record["created"])
            | set(record["stages"].values()) | set(record["foreign_proxies"])
            | ({record["config_update"]["stage"], record["config_update"]["backup"]} if record.get("config_update") else set()))


def folder_identities(paths):
    endpoints, ancestors = set(), set()
    for path in set(paths):
        try:
            directory = Directory(path)
        except FileNotFoundError:
            continue
        with ExitStack() as stack:
            stack.enter_context(directory)
            identity = directory.identity()
            endpoints.add(identity)
            seen = set()
            while identity not in seen:
                seen.add(identity)
                ancestors.add(identity)
                directory = stack.enter_context(directory.parent())
                identity = directory.identity()
    return endpoints, ancestors


class Installer:
    def __init__(self, store, find_game, launch_executables=None):
        self.store = store
        self.find_game = find_game
        self.launch_executables = launch_executables or (lambda value: ())

    def _check_folders(self, game, records, api_path=None):
        if not records:
            return
        candidates = [game["game_directory"]] + ([api_path] if api_path else [])
        identities, ancestors = folder_identities(candidates)
        for record in records:
            roots = [record["game_dir"]]
            if "game_directory" in record:
                roots.append(record["game_directory"])
            try:
                other = self.find_game(record["app_id"])
                if inside_game(other, record):
                    roots.append(other["game_directory"])
            except (ValueError, OSError):
                # A missing manifest cannot release the recorded files.
                pass
            if any(folders_overlap(candidate, root) for candidate in candidates for root in roots):
                raise ValueError(f"Game folder overlaps the active installation for app {record['app_id']}")
            saved, saved_ancestors = folder_identities(roots)
            if identities & saved_ancestors or saved & ancestors:
                raise ValueError(f"Game folder overlaps the active installation for app {record['app_id']}")

    def _check_shared_files(self, directory, records):
        names = {*APIS, CONFIG, *(x.replace(".dll", "_o.dll") for x in APIS),
                 *(x + ".dll" for x in PROXIES)}
        names = {name.casefold() for name in names}
        files = {(info.st_dev, info.st_ino) for name, info in directory.entries()
                 if name.casefold() in names and stat.S_ISREG(info.st_mode)}
        for record in records:
            try:
                saved = Directory(record["game_dir"])
            except FileNotFoundError:
                continue
            with saved:
                owned = {name.casefold() for name in recorded_names(record)}
                shared = saved.identity() == directory.identity()
                shared |= any((info.st_dev, info.st_ino) in files for name, info in saved.entries()
                              if name.casefold() in owned and stat.S_ISREG(info.st_mode))
                for name in record["quarantine"]:
                    try:
                        capture = saved.child(name)
                    except FileNotFoundError:
                        continue
                    with capture:
                        shared |= any((info.st_dev, info.st_ino) in files for _, info in capture.entries()
                                      if stat.S_ISREG(info.st_mode))
                if shared:
                    raise ValueError(f"Game files overlap the active installation for app {record['app_id']}")

    def install(self, value, mode, proxy, enabled, original_launch, manual_launch=False):
        with LOCK:
            value = app_id(value)
            records = self.store.all()
            if any(r["app_id"] == value and r["mode"] == "proxy" and r["launch_restore"] == "pending" for r in records):
                raise ValueError("Launch options restoration is pending. Restore them or explicitly resolve by hand first")
            if any(r["app_id"] == value and r["phase"] != "removed" for r in records):
                raise ValueError("Remove or recover this game's installation first")
            for record in records:
                if record["phase"] != "removed" and "game_directory" not in record:
                    raise ValueError(f"Cannot confirm the folder of app {record['app_id']}. Reinstall or remove it first")
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
            active = [record for record in records if record["phase"] != "removed"]
            self._check_folders(game, active)
            resources = verified_resources()
            with Directory(game["game_directory"]) as root:
                anti_cheat(root)
            path = api_directory(game["game_directory"], launches=lambda: self.launch_executables(value))
            if mode == "proxy" and path != game["game_directory"]:
                raise ValueError("Proxy mode needs the Steam API DLL in the game's top folder. Use direct mode")
            with Directory(path) as directory:
                self._check_folders(game, active, path)
                self._check_shared_files(directory, active)
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
                          "game_directory": game["game_directory"],
                          "mode": mode, "proxy_name": proxy, "originals": originals,
                          "installed": {name: digest(data) for name, data in payload.items()},
                          "created": created, "stages": {name: ".cdh-" + uuid.uuid4().hex for name in payload},
                          "phase": "installing", "original_launch_options": original_launch,
                          "set_launch_options": desired, "enabled_dlc_ids": enabled,
                          "quarantine": {}, "launch_restore": "acknowledged",
                          "launch_setup": "pending" if mode == "proxy" else "acknowledged",
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

    def save_dlc_selection(self, value, enabled):
        with LOCK:
            value = app_id(value)
            if not isinstance(enabled, list) or len(enabled) > 1000:
                raise ValueError("Invalid DLC list")
            enabled = sorted({app_id(x) for x in enabled})
            record = self.store.load(value)
            if not record or record["phase"] != "installed":
                raise ValueError("An installed game is required")
            game = self.find_game(value)
            if not inside_game(game, record):
                raise ValueError("Manifest game directory changed. Inspect the record manually")
            assert_game_idle(game)
            with Directory(game["game_directory"]) as root:
                anti_cheat(root)
            with Directory(record["game_dir"]) as directory:
                directory.bind_journal(record, self.store.save)
                self._recover_config(record, directory)
                if self.status(value)[0] != "installed":
                    raise ValueError("Installation changed. Recover it before saving DLC")
                original = directory.read(CONFIG)
                config = json.loads(original)
                config["override_dlc_status"] = {x: "unlocked" for x in enabled}
                payload = json.dumps(config, indent=2).encode()
                if record["enabled_dlc_ids"] == enabled and digest(payload) == record["installed"][CONFIG]:
                    return record
                directory.rename_preflight()
                update = {"old": record["installed"][CONFIG], "new": digest(payload), "ids": enabled,
                          "stage": ".cdh-" + uuid.uuid4().hex, "backup": ".cdh-" + uuid.uuid4().hex,
                          "committed": False}
                update["writes"] = {
                    update[role]: {"began": True, "sha256": digest(data), "size": len(data),
                                   "payload": data.decode("utf-8"), "partial_size": None}
                    for role, data in (("stage", payload), ("backup", original))}
                record["config_update"] = update
                self.store.save(record)
                try:
                    directory.write_new(update["stage"], payload)
                    directory.write_new(update["backup"], original)
                    current_game = self.find_game(value)
                    if not inside_game(current_game, record):
                        raise ValueError("Manifest game directory changed. Inspect the record manually")
                    assert_game_idle(current_game, ignored_fds=(getattr(directory, "fd", None),))
                    directory.delete(CONFIG, update["old"])
                    directory.rename(update["stage"], CONFIG, update["new"])
                    record["installed"][CONFIG] = update["new"]
                    record["enabled_dlc_ids"] = enabled
                    update["committed"] = True
                    self.store.save(record)
                except BaseException:
                    # Reload the durable decision, including an ambiguous settings fsync failure.
                    saved = self.store.load(value)
                    directory.bind_journal(saved, self.store.save)
                    self._recover_config(saved, directory)
                    raise
                self._recover_config(record, directory)
            return record

    def _recover_config(self, record, directory):
        update = record.get("config_update")
        if not update:
            return
        game = self.find_game(record["app_id"])
        if not inside_game(game, record):
            raise ValueError("Manifest game directory changed. Inspect the record manually")
        assert_game_idle(game, ignored_fds=(getattr(directory, "fd", None),))
        warnings = directory.recover_quarantine()
        if warnings:
            raise ValueError("; ".join(warnings))
        known = {*record["stages"].values(), update["stage"], update["backup"], *record["quarantine"]}
        if any(name.startswith(".cdh-") and name not in known for name in directory.names()):
            raise ValueError("Unrecorded config staging file preserved")
        # A recorded prefix is ours, but its observed hash must also survive
        # quarantine journaling before deletion can be retried after a crash.
        partials = []
        for role, sha in (("stage", update["new"]), ("backup", update["old"])):
            name = update[role]
            try:
                current = directory.hash(name)
            except (OSError, ValueError) as error:
                raise ValueError("Unsafe config staging file preserved") from error
            if current is None or current == sha:
                continue
            intent = update.get("writes", {}).get(name)
            data = directory.read(name)
            if (not intent or not intent["began"] or len(data) >= intent["size"]
                    or not intent["payload"].encode("utf-8").startswith(data)):
                raise ValueError("Changed config staging file preserved")
            partials.append((name, intent, data))
        for name, intent, data in partials:
            intent["partial_size"] = len(data)
            self.store.save(record)
            directory.delete(name, digest(data))
        current = directory.hash(CONFIG)
        if update["committed"]:
            if current != update["new"]:
                raise ValueError("Config changed during update; preserved for recovery")
        else:
            if current == update["new"] and directory.hash(update["backup"]) == update["old"]:
                directory.rename(CONFIG, update["stage"], update["new"])
                current = None
            if current is None and directory.hash(update["backup"]) == update["old"]:
                directory.rename(update["backup"], CONFIG, update["old"])
            elif current != update["old"]:
                raise ValueError("Config changed during update; preserved for recovery")
        for name, sha in ((update["stage"], update["new"]), (update["backup"], update["old"])):
            current = directory.hash(name)
            if current is not None:
                if current != sha:
                    raise ValueError("Changed config staging file preserved for recovery")
                directory.delete(name, sha)
        del record["config_update"]
        self.store.save(record)

    def resolve_setup(self, value, manual=False):
        with LOCK:
            value = app_id(value)
            record = self.store.load(value)
            if not record or record["phase"] != "installed" or record["mode"] != "proxy":
                raise ValueError("An installed proxy is required")
            if type(manual) is not bool or (record["set_launch_options"] is None and not manual):
                raise ValueError("Confirm manual launch setup")
            self.find_game(value)
            record["launch_setup"] = "manual" if manual else "acknowledged"
            self.store.save(record)
            return record

    def status(self, value):
        with LOCK:
            record = self.store.load(app_id(value))
            if not record or record["phase"] == "removed":
                return "not installed", record
            game = self.find_game(value)
            if not inside_game(game, record):
                return "game directory changed", record
            with Directory(record["game_dir"]) as directory:
                if record.get("config_update"):
                    return "interrupted DLC update; use Save or Remove", record
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
                directory.bind_journal(record, self.store.save)
                self._recover_config(record, directory)
                warnings = self._remove(record, directory)
            return record, warnings

    def resolve_launch(self, value, manual=False):
        with LOCK:
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
