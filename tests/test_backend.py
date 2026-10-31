from __future__ import annotations

import concurrent.futures
import asyncio
from contextlib import ExitStack
import copy
import errno
import json
import os
import posixpath
import stat
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "py_modules"))
sys.path.insert(0, str(BASE))

import decky
import install_state
import main
import metadata
import resource_files
import safe_fs
import smokeapi
import steam_library
import vdf_lite
from fs_adapter import TestDirectory


class BackendTests(unittest.TestCase):
    maxDiff = 2000
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cdh-test-", dir=BASE)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.settings = self.base / "settings"
        self.settings.mkdir()
        self.home = self.base / "home"
        self.library = self.home / ".local" / "share" / "Steam" / "steamapps"
        self.game = self.library / "common" / "Example"
        self.game.mkdir(parents=True)
        self.manifest("Example")
        self.proc = self.base / "proc"
        self.proc.mkdir()
        idle = steam_library.assert_game_idle
        patcher = patch.object(smokeapi, "assert_game_idle",
                               side_effect=lambda game, **kw: idle(game, proc_path=str(self.proc), **kw))
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.game / "steam_api.dll").write_bytes(b"original 32-bit DLL")
        (self.game / "steam_api64.dll").write_bytes(b"original 64-bit DLL")
        (self.game / "unrelated.txt").write_bytes(b"leave this alone")
        self.before = self.snapshot()
        for module in (install_state, resource_files, smokeapi, steam_library):
            patcher = patch.object(module, "Directory", TestDirectory)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("DECKY_USER_HOME", str(self.home)), ("DECKY_PLUGIN_SETTINGS_DIR", str(self.settings))):
            patcher = patch.object(decky, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = install_state.Store(str(self.settings))
        self.engine = smokeapi.Installer(self.store, steam_library.find_game)
        self.store.allow("123", True)

    def manifest(self, install_dir, value="123"):
        # Escape backslashes in Valve's quoted-string syntax.
        install_dir = install_dir.replace("\\", "\\\\")
        (self.library / f"appmanifest_{value}.acf").write_text(
            f'"AppState" {{ "appid" "{value}" "name" "Example" "installdir" "{install_dir}" }}')

    def snapshot(self, folder=None):
        folder = self.game if folder is None else folder
        return {x.name: x.read_bytes() for x in folder.iterdir() if x.is_file() and not x.is_symlink()}

    def tree_snapshot(self, folder=None):
        folder = self.game if folder is None else folder
        result = {}
        for path in folder.rglob("*"):
            info = path.lstat()
            data = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            result[path.relative_to(folder).as_posix()] = (info.st_mode, info.st_dev, info.st_ino, data)
        return result

    def other_game(self):
        other = self.game.parent / "Other"
        other.mkdir()
        (other / "steam_api64.dll").write_bytes(b"other original DLL")
        (other / "unrelated.txt").write_bytes(b"other owner file")
        self.manifest("Other", "456")
        self.store.allow("456", True)
        return other

    def install(self, mode="direct", original=""):
        return self.engine.install("123", mode, "version", [], original)

    def test_manifest_absolute_traversal_and_separators_rejected(self):
        for name in ("/etc", "../outside", "..", "nested/game", "nested\\game", "C:\\Windows"):
            with self.subTest(name=name):
                self.manifest(name)
                with self.assertRaises(ValueError):
                    steam_library.find_game("123")
        self.assertEqual(self.before, self.snapshot())

    def test_invalid_appids_rejected_before_network_or_files(self):
        for value in ("../123", "123/4", "-1", "1;rm", "１２３", 123, "", "0"):
            with self.subTest(value=value), patch.object(metadata, "fetch") as fetch:
                with self.assertRaises(ValueError):
                    steam_library.find_game(value)
                with self.assertRaises(ValueError):
                    metadata.lookup(value)
                fetch.assert_not_called()

    def test_symlinked_game_directory_rejected(self):
        link = self.game.parent / "Link"
        link.symlink_to(self.game, target_is_directory=True)
        self.manifest("Link")
        with self.assertRaises((ValueError, OSError)):
            self.install()
        self.assertEqual(self.before, self.snapshot())

    def test_symlinked_target_file_rejected(self):
        victim = self.base / "victim"
        victim.write_bytes(b"external bytes")
        (self.game / "SmokeAPI.config.json").symlink_to(victim)
        with self.assertRaises((ValueError, OSError)):
            self.install()
        self.assertEqual(b"external bytes", victim.read_bytes())
        self.assertEqual(self.before, self.snapshot())

    def test_symlinked_settings_rejected(self):
        victim = self.base / "victim.json"
        victim.write_text("[]")
        (self.settings / "allowlist.json").unlink()
        (self.settings / "allowlist.json").symlink_to(victim)
        with self.assertRaises((ValueError, OSError)):
            self.install()
        self.assertEqual("[]", victim.read_text())

    def test_missing_namespaced_settings_directory_is_created_safely(self):
        path = self.base / "fresh-settings"
        store = install_state.Store(str(path))
        self.assertTrue(path.is_dir())
        self.assertEqual([], store.allowlist())

    def test_malicious_proxy_state_rejected_before_any_delete(self):
        record = self.install()
        before = self.snapshot()
        record["proxy_name"] = "../../outside/victim"
        (self.settings / "123.json").write_text(json.dumps(record))
        with patch.object(TestDirectory, "delete", side_effect=AssertionError("must not delete")):
            with self.assertRaises(ValueError):
                self.engine.uninstall("123")
        self.assertEqual(before, self.snapshot())

    def test_normal_install_uninstall_byte_exact(self):
        self.install()
        config = json.loads((self.game / install_state.CONFIG).read_text())
        self.assertEqual("original", config["default_app_status"])
        self.assertEqual({}, config["override_dlc_status"])
        self.assertFalse(config["auto_inject_inventory"])
        self.assertNotIn("default_dlc_status", config)
        self.assertEqual("installed", self.engine.status("123")[0])
        record, warnings = self.engine.uninstall("123")
        self.assertEqual([], warnings)
        self.assertEqual("removed", record["phase"])
        self.assertEqual(self.before, self.snapshot())

    def test_normal_proxy_uninstall_preserves_original(self):
        (self.game / "steam_api.dll").unlink()
        before = self.snapshot()
        record = self.install("proxy", "/home/deck/launch.sh %command%")
        self.assertEqual('WINEDLLOVERRIDES="version=n,b" /home/deck/launch.sh %command%', record["set_launch_options"])
        self.assertEqual(b"original 64-bit DLL", (self.game / "steam_api64.dll").read_bytes())
        self.assertEqual([], self.engine.uninstall("123")[1])
        self.assertEqual(before, self.snapshot())

    def test_direct_removal_after_external_launch_edit_needs_no_restore(self):
        first = self.install(original="original %command%")
        self.assertIsNone(first["set_launch_options"])
        external = "luma-wrapper %command%"
        record, warnings = self.engine.uninstall("123")
        self.assertEqual([], warnings)
        self.assertEqual("acknowledged", record["launch_restore"])
        self.assertEqual("acknowledged", self.store.load("123")["launch_restore"])
        second = self.install(original=external)
        self.assertIsNone(second["set_launch_options"])
        self.assertEqual(external, second["original_launch_options"])

    def test_old_direct_pending_record_does_not_block_reinstall(self):
        record = self.install()
        self.engine.uninstall("123")
        record.update(phase="removed", launch_restore="pending")
        record.pop("foreign_proxies")
        (self.settings / "123.json").write_text(json.dumps(record))
        self.assertEqual({}, self.store.load("123")["foreign_proxies"])
        self.assertEqual("acknowledged", self.store.load("123")["launch_restore"])
        self.install(original="luma-wrapper %command%")

    def test_old_proxy_pending_record_still_requires_restore(self):
        (self.game / "steam_api.dll").unlink()
        self.install("proxy")
        record, _ = self.engine.uninstall("123")
        record.pop("foreign_proxies")
        (self.settings / "123.json").write_text(json.dumps(record))
        self.assertEqual("pending", self.store.load("123")["launch_restore"])
        with self.assertRaisesRegex(ValueError, "restoration is pending"):
            self.install()

    def test_post_update_uninstall_keeps_new_dll_and_backup(self):
        self.install()
        current = self.game / "steam_api64.dll"
        backup = self.game / "steam_api64_o.dll"
        current.write_bytes(b"new game update")
        saved = backup.read_bytes()
        self.assertIn(smokeapi.CHANGED, self.engine.status("123")[0])
        _, warnings = self.engine.uninstall("123")
        self.assertTrue(warnings)
        self.assertEqual(b"new game update", current.read_bytes())
        self.assertEqual(saved, backup.read_bytes())
        self.assertFalse((self.game / install_state.CONFIG).exists())

    def test_modified_config_is_left_alone(self):
        self.install()
        (self.game / install_state.CONFIG).write_bytes(b"owner config")
        _, warnings = self.engine.uninstall("123")
        self.assertTrue(warnings)
        self.assertEqual(b"owner config", (self.game / install_state.CONFIG).read_bytes())

    def test_foreign_backups_refused_without_mutation(self):
        for name in ("steam_api_o.dll", "steam_api64_o.dll"):
            with self.subTest(name=name):
                file = self.game / name
                file.write_bytes(b"foreign backup")
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.install()
                self.assertEqual(before, self.snapshot())
                file.unlink()

    def test_failed_copy_mid_install_rolls_back_exactly(self):
        original = TestDirectory.write_new
        count = 0
        failure_fired = False
        rollback_writes = []
        failure = OSError("simulated copy failure")
        def fail(directory, name, data):
            nonlocal count, failure_fired
            path = getattr(directory, "path", None)
            # Store writes occur in settings; fail the second game staging copy.
            if path is not None:
                is_game = path == self.game
            else:
                current = os.fstat(directory.fd)
                game = os.stat(self.game)
                is_game = (current.st_dev, current.st_ino) == (game.st_dev, game.st_ino)
            if is_game:
                if failure_fired:
                    rollback_writes.append((name, data))
                else:
                    count += 1
                    if count == 2:
                        failure_fired = True
                        raise failure
            return original(directory, name, data)
        with patch.object(TestDirectory, "write_new", fail):
            with self.assertRaises(OSError) as error:
                self.install()
        self.assertIs(failure, error.exception)
        self.assertTrue(failure_fired)
        self.assertEqual(2, count)
        self.assertEqual(self.before, self.snapshot())
        self.assertEqual("removed", self.store.load("123")["phase"])
        # No original DLL moved before this failure; rollback only removes stages.
        self.assertEqual([], rollback_writes)

    def test_failed_commit_mid_install_rolls_back_exactly(self):
        original = TestDirectory.rename
        count = 0
        def fail(directory, source, target, expected):
            nonlocal count
            count += 1
            if count == 4:
                raise OSError("simulated rename failure")
            return original(directory, source, target, expected)
        with patch.object(TestDirectory, "rename", fail):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(self.before, self.snapshot())

    def test_resource_hash_mismatch_refuses(self):
        pins = dict(resource_files.PINS)
        pins["steam_api.dll"] = "0" * 64
        with patch.object(resource_files, "PINS", pins):
            with self.assertRaisesRegex(ValueError, "resource hash mismatch"):
                self.install()
        self.assertEqual(self.before, self.snapshot())

    def test_allowlist_default_empty_and_confirmation_required(self):
        (self.settings / "allowlist.json").unlink()
        self.assertEqual([], self.store.allowlist())
        with self.assertRaisesRegex(ValueError, "owner allowlist"):
            self.install()
        with self.assertRaises(ValueError):
            self.store.allow("123", False)
        self.assertEqual(self.before, self.snapshot())

    def test_anticheat_markers_case_insensitive_two_levels(self):
        markers = ("EaSyAnTiChEaT", "EasyAntiCheat_x64.dll", "EasyAntiCheat_EOS_Setup.exe",
                   "BattlEye", "BEClient_x64.dll", "BEService_x64.exe", "start_protected_game.exe")
        nested = self.game / "bin"
        nested.mkdir()
        for parent in (self.game, nested):
            for name in markers:
                with self.subTest(parent=parent.name, name=name):
                    file = parent / name
                    file.write_bytes(b"marker")
                    with self.assertRaisesRegex(ValueError, "Anti-cheat"):
                        self.install()
                    file.unlink()

    def test_nested_anticheat_markers_refuse_without_mutation(self):
        for depth in (3, 16):
            folder = self.game.joinpath(*(f"d{n}" for n in range(depth)))
            folder.mkdir(parents=True, exist_ok=True)
            for name in ("EasyAntiCheat_x64.dll", "eAsYaNtIcHeAt_X64.DlL", "bEsErViCe_test.ExE"):
                with self.subTest(depth=depth, name=name):
                    file = folder / name
                    file.write_bytes(b"marker")
                    before = self.tree_snapshot()
                    with patch.object(self.store, "save") as save, patch.object(TestDirectory, "rename_preflight") as probe:
                        with self.assertRaisesRegex(ValueError, "Anti-cheat marker"):
                            self.install()
                        save.assert_not_called()
                        probe.assert_not_called()
                    self.assertEqual(before, self.tree_snapshot())
                    file.unlink()

    def test_anticheat_truncated_scan_refuses_without_mutation(self):
        folder = self.game.joinpath(*(f"d{n}" for n in range(17)))
        folder.mkdir(parents=True)
        (folder / "hidden.txt").write_bytes(b"unscanned")
        before = self.tree_snapshot()
        with patch.object(self.store, "save") as save, patch.object(TestDirectory, "rename_preflight") as probe:
            with self.assertRaisesRegex(ValueError, "Anti-cheat scan incomplete.*16-folder limit"):
                self.install()
            save.assert_not_called()
            probe.assert_not_called()
        self.assertEqual(before, self.tree_snapshot())

    def test_nested_scan_symlink_and_traversal_failure_refuse_without_mutation(self):
        folder = self.game / "a" / "b" / "c"
        folder.mkdir(parents=True)
        link = folder / "alias"
        link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.install()
        link.unlink()
        before = self.tree_snapshot()
        entries = TestDirectory.entries
        def denied(directory):
            if "c" in directory.names():
                raise PermissionError("scan denied")
            return entries(directory)
        with patch.object(TestDirectory, "entries", denied), patch.object(self.store, "save") as save:
            with self.assertRaisesRegex(PermissionError, "scan denied"):
                self.install()
            save.assert_not_called()
        self.assertEqual(before, self.tree_snapshot())

    def test_malformed_state_and_allowlist_refuse_mutation(self):
        for data in ("{", "{}", "[]", '{"schema": 1}'):
            with self.subTest(data=data):
                (self.settings / "123.json").write_text(data)
                with self.assertRaises((ValueError, TypeError)):
                    self.install()
                with self.assertRaises((ValueError, TypeError)):
                    self.store.allow("123", True)
                self.assertEqual(self.before, self.snapshot())
        (self.settings / "123.json").unlink()
        (self.settings / "allowlist.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.install()

    def test_state_schema_rejects_unexpected_paths_and_hashes(self):
        record = self.install()
        for field, bad in (("proxy_name", "/absolute"), ("created", ["../victim"]),
                           ("installed", {"victim": "0" * 64}), ("stages", {}),
                           ("originals", {"steam_api.dll": "bad"})):
            with self.subTest(field=field):
                altered = copy.deepcopy(record)
                altered[field] = bad
                with self.assertRaises(ValueError):
                    install_state.validate(altered, "123")

    def test_state_save_failure_rolls_back_game_files(self):
        save = self.store.save
        count = 0
        def fail(record):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("settings full")
            save(record)
        with patch.object(self.store, "save", fail):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(self.before, self.snapshot())

    def test_persistent_settings_failure_stops_before_unjournaled_moves(self):
        save = self.store.save
        count = 0
        def fail(record):
            nonlocal count
            count += 1
            if count >= 2:
                raise OSError("settings remain full")
            save(record)
        with patch.object(self.store, "save", fail):
            with self.assertRaises(OSError):
                self.install()
        for name, data in self.before.items():
            self.assertEqual(data, (self.game / name).read_bytes())
        self.assertEqual("installing", self.store.load("123")["phase"])
        self.assertEqual([], self.engine.uninstall("123")[1])
        self.assertEqual(self.before, self.snapshot())

    def test_delete_and_rename_races_preserve_new_bytes(self):
        for operation in ("delete", "rename"):
            with self.subTest(operation=operation):
                source, target = "race-source", "race-target"
                (self.game / source).write_bytes(b"hashed bytes")
                original_hash = TestDirectory.hash
                changed = False
                journal = {"quarantine": {}}
                saved = []
                def swap_after_hash(directory, name):
                    nonlocal changed
                    result = original_hash(directory, name)
                    if name == "capture" and not changed:
                        changed = True
                        (self.game / source).write_bytes(b"NEW GAME UPDATE")
                    return result
                with TestDirectory(str(self.game)) as directory:
                    directory.bind_journal(journal, lambda record: saved.append(copy.deepcopy(record)))
                    with patch.object(TestDirectory, "hash", swap_after_hash):
                        if operation == "delete":
                            directory.delete(source, safe_fs.digest(b"hashed bytes"))
                        else:
                            directory.rename(source, target, safe_fs.digest(b"hashed bytes"))
                self.assertTrue(changed)
                self.assertTrue(saved[0]["quarantine"])
                self.assertEqual({}, journal["quarantine"])
                self.assertEqual(b"NEW GAME UPDATE", (self.game / source).read_bytes())
                if operation == "rename":
                    self.assertEqual(b"hashed bytes", (self.game / target).read_bytes())
                    (self.game / target).unlink()
                (self.game / source).unlink()

    def test_capture_mismatch_never_deletes_or_restores_unchecked_bytes(self):
        for operation in ("delete", "rename"):
            with self.subTest(operation=operation), TestDirectory(str(self.game)) as directory:
                (self.game / "race-source").write_bytes(b"NEW GAME UPDATE")
                with self.assertRaisesRegex(ValueError, "Changed file preserved"):
                    if operation == "delete":
                        directory.delete("race-source", safe_fs.digest(b"old bytes"))
                    else:
                        directory.rename("race-source", "race-target", safe_fs.digest(b"old bytes"))
                self.assertEqual(b"NEW GAME UPDATE", (self.game / "race-source").read_bytes())
                self.assertFalse((self.game / "race-target").exists())
                (self.game / "race-source").unlink()

    def test_uninstall_capture_races_preserve_replacements(self):
        for source in (install_state.CONFIG, "steam_api64_o.dll"):
            with self.subTest(source=source):
                self.install()
                raw = TestDirectory.move_to
                changed = False
                def swap(directory, name, destination, target):
                    nonlocal changed
                    if name == source and target == "capture" and not changed:
                        changed = True
                        (self.game / source).write_bytes(b"NEW GAME UPDATE")
                    return raw(directory, name, destination, target)
                with patch.object(TestDirectory, "move_to", swap):
                    _, warnings = self.engine.uninstall("123")
                self.assertTrue(changed)
                self.assertTrue(warnings)
                self.assertEqual(b"NEW GAME UPDATE", (self.game / source).read_bytes())
                if source.endswith("_o.dll"):
                    self.assertFalse((self.game / "steam_api64.dll").exists())
                # Restore this fixture explicitly so each race uses a clean record.
                for path in self.game.iterdir():
                    if path.is_file():
                        path.unlink()
                for name, data in self.before.items():
                    (self.game / name).write_bytes(data)
                (self.settings / "123.json").unlink()

    def test_quarantine_mismatch_collision_preserved_and_warned_after_restart(self):
        self.install()
        raw = TestDirectory.move_to
        changed = False
        def swap(directory, source, destination, target):
            nonlocal changed
            if source == install_state.CONFIG and target == "capture" and not changed:
                changed = True
                (self.game / source).write_bytes(b"NEW GAME UPDATE")
                raw(directory, source, destination, target)
                (self.game / source).write_bytes(b"ANOTHER GAME UPDATE")
                return
            return raw(directory, source, destination, target)
        with patch.object(TestDirectory, "move_to", swap):
            record, warnings = self.engine.uninstall("123")
        self.assertTrue(warnings)
        quarantine = next(iter(record["quarantine"]))
        self.assertEqual(b"NEW GAME UPDATE", (self.game / quarantine / "capture").read_bytes())
        self.assertEqual(b"ANOTHER GAME UPDATE", (self.game / install_state.CONFIG).read_bytes())
        persisted = self.store.load("123")["quarantine"][quarantine]["warning"]
        self.assertIn(quarantine, persisted)
        self.assertIn(install_state.CONFIG, persisted)
        restarted = smokeapi.Installer(install_state.Store(str(self.settings)), steam_library.find_game)
        self.assertTrue(restarted.uninstall("123")[1])
        self.assertEqual(b"NEW GAME UPDATE", (self.game / quarantine / "capture").read_bytes())

    def test_journaled_quarantine_recovers_all_crash_boundaries(self):
        for boundary in ("before_capture", "after_capture", "after_final", "after_delete"):
            with self.subTest(boundary=boundary):
                self.install()
                move_to, raw_delete = TestDirectory.move_to, TestDirectory.raw_delete
                crashed = False
                def crash_rename(directory, source, destination, target):
                    nonlocal crashed
                    crash = not crashed and ((boundary in ("before_capture", "after_capture")
                                              and target == "capture")
                                             or (boundary == "after_final" and source == "capture"))
                    if crash and boundary == "before_capture":
                        crashed = True
                        raise RuntimeError("crash")
                    move_to(directory, source, destination, target)
                    if crash:
                        crashed = True
                        raise RuntimeError("crash")
                def crash_delete(directory, name):
                    nonlocal crashed
                    raw_delete(directory, name)
                    if boundary == "after_delete" and not crashed:
                        crashed = True
                        raise RuntimeError("crash")
                with patch.object(TestDirectory, "move_to", crash_rename), patch.object(TestDirectory, "raw_delete", crash_delete):
                    with self.assertRaisesRegex(RuntimeError, "crash"):
                        self.engine.uninstall("123")
                self.assertTrue(self.store.load("123")["quarantine"])
                restarted = smokeapi.Installer(install_state.Store(str(self.settings)), steam_library.find_game)
                record, warnings = restarted.uninstall("123")
                self.assertEqual([], warnings)
                self.assertEqual({}, record["quarantine"])
                self.assertEqual(self.before, self.snapshot())
                self.engine.resolve_launch("123", manual=True)

    def test_unsupported_rename_flags_refused_before_staging(self):
        raw = TestDirectory.raw_rename
        for code in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            def unsupported(directory, source, target):
                if source == "source":
                    raise OSError(code, "unsupported rename flag")
                return raw(directory, source, target)
            with self.subTest(code=code), patch.object(TestDirectory, "raw_rename", unsupported), patch.object(self.store, "save") as save:
                with self.assertRaisesRegex(RuntimeError, "Incompatible filesystem .*RENAME_NOREPLACE"):
                    self.install()
                save.assert_not_called()
                self.assertEqual(self.before, self.snapshot())

    def test_unsupported_directory_sync_has_compatibility_error_and_no_leftovers(self):
        def unsupported(directory):
            path = (directory.path if hasattr(directory, "path")
                    else Path(os.readlink(f"/proc/self/fd/{directory.fd}")))
            if path.name.startswith(".cdh-probe-"):
                raise OSError(errno.EINVAL, "unsupported directory sync")
        with patch.object(TestDirectory, "sync", unsupported), patch.object(self.store, "save") as save:
            with self.assertRaisesRegex(RuntimeError, "Incompatible filesystem .*durable directory operations"):
                self.install()
            save.assert_not_called()
        self.assertEqual(self.before, self.snapshot())

    def test_launch_restore_failure_blocks_reinstall_and_retains_original(self):
        (self.game / "steam_api.dll").unlink()
        original = "/home/deck/wrapper %command%"
        first = self.install("proxy", original)
        record, warnings = self.engine.uninstall("123")
        self.assertEqual([], warnings)
        self.assertEqual("removed", record["phase"])
        self.assertEqual("pending", record["launch_restore"])
        restarted = smokeapi.Installer(install_state.Store(str(self.settings)), steam_library.find_game)
        with self.assertRaisesRegex(ValueError, "restoration is pending"):
            restarted.install("123", "direct", "version", [], first["set_launch_options"])
        retained = self.store.load("123")
        self.assertEqual(original, retained["original_launch_options"])
        self.assertEqual("pending", retained["launch_restore"])
        restarted.resolve_launch("123")
        self.assertEqual("acknowledged", self.store.load("123")["launch_restore"])
        restarted.install("123", "direct", "version", [], original)

    def test_quarantine_reservation_collision_never_adopts_existing_bytes(self):
        name = ".cdh-q-" + "a" * 32
        fixed_uuid = types.SimpleNamespace(hex="a" * 32)
        for kind in ("file", "directory"):
            with self.subTest(kind=kind), TestDirectory(str(self.game)) as directory:
                path = self.game / name
                if kind == "directory":
                    path.mkdir()
                    existing = path / "capture"
                else:
                    existing = path
                existing.write_bytes(b"hashed bytes")
                journal = {"quarantine": {}}
                directory.bind_journal(journal, lambda _: None)
                with patch.object(safe_fs.uuid, "uuid4", return_value=fixed_uuid):
                    with self.assertRaises(FileExistsError):
                        directory.delete("steam_api.dll", safe_fs.digest(b"hashed bytes"))
                self.assertEqual([], directory.recover_quarantine())
                self.assertEqual({}, journal["quarantine"])
                self.assertEqual(b"hashed bytes", existing.read_bytes())
                self.assertEqual(self.before["steam_api.dll"], (self.game / "steam_api.dll").read_bytes())
                existing.unlink()
                if kind == "directory":
                    path.rmdir()

    def test_failed_capture_does_not_adopt_occupied_destination(self):
        raw = TestDirectory.move_to
        occupied = []
        def collision(directory, source, destination, target):
            if hasattr(destination, "path"):
                path = destination.path
            else:
                path = Path(os.readlink(f"/proc/self/fd/{destination.fd}"))
            (path / target).write_bytes(self.before["steam_api.dll"])
            occupied.append(path / target)
            return raw(directory, source, destination, target)
        with TestDirectory(str(self.game)) as directory:
            journal = {"quarantine": {}}
            directory.bind_journal(journal, lambda _: None)
            with patch.object(TestDirectory, "move_to", collision):
                with self.assertRaises(FileExistsError):
                    directory.delete("steam_api.dll", safe_fs.digest(self.before["steam_api.dll"]))
            self.assertEqual([], directory.recover_quarantine())
            self.assertEqual({}, journal["quarantine"])
        self.assertEqual(self.before["steam_api.dll"], occupied[0].read_bytes())
        self.assertEqual(self.before, self.snapshot())

    def test_crash_between_journal_and_capture_recovers_empty_reservation(self):
        record = self.install()
        with TestDirectory(str(self.game)) as directory:
            directory.bind_journal(record, self.store.save)
            with patch.object(TestDirectory, "move_to", side_effect=RuntimeError("crash before capture")):
                with self.assertRaisesRegex(RuntimeError, "crash before capture"):
                    directory.delete(install_state.CONFIG, record["installed"][install_state.CONFIG])
        pending = self.store.load("123")
        name, entry = next(iter(pending["quarantine"].items()))
        info = (self.game / name).stat()
        self.assertEqual((info.st_dev, info.st_ino), (entry["dev"], entry["ino"]))
        self.assertEqual([], list((self.game / name).iterdir()))
        self.assertTrue((self.game / install_state.CONFIG).exists())
        self.assertEqual([], self.engine.uninstall("123")[1])
        self.assertFalse((self.game / name).exists())
        self.assertEqual(self.before, self.snapshot())

    def test_recovery_requires_reserved_directory_identity(self):
        for identity in ("mismatch", "missing", "legacy"):
            with self.subTest(identity=identity), TestDirectory(str(self.game)) as directory:
                name = ".cdh-q-" + "b" * 32
                path = self.game / name
                path.mkdir()
                (path / "capture").write_bytes(b"hashed bytes")
                info = path.stat()
                entry = {"source": "source", "target": None,
                         "expected": safe_fs.digest(b"hashed bytes"), "warning": ""}
                if identity != "legacy":
                    entry.update(dev=info.st_dev, ino=info.st_ino + 1)
                if identity == "missing":
                    (path / "capture").unlink()
                    path.rmdir()
                journal = {"quarantine": {name: entry}}
                directory.bind_journal(journal, lambda _: None)
                warnings = directory.recover_quarantine()
                if identity == "missing":
                    self.assertEqual([], warnings)
                    self.assertEqual({}, journal["quarantine"])
                else:
                    self.assertTrue(warnings)
                    self.assertEqual(b"hashed bytes", (path / "capture").read_bytes())
                    (path / "capture").unlink()
                    path.rmdir()

    def test_partial_probe_enospc_preserves_original_error_and_cleans_all_entries(self):
        write = TestDirectory.write_new
        original = OSError(errno.ENOSPC, "partial probe write")
        def partial(directory, name, data):
            path = (directory.path if hasattr(directory, "path")
                    else Path(os.readlink(f"/proc/self/fd/{directory.fd}")))
            if path.name.startswith(".cdh-probe-"):
                (path / name).write_bytes(b"ren")
                (path / ".cdh-q-partial").write_bytes(b"partial")
                raise original
            return write(directory, name, data)
        with TestDirectory(str(self.game)) as directory:
            with patch.object(TestDirectory, "write_new", partial), \
                    patch.object(TestDirectory, "raw_rename", side_effect=OSError(errno.EINVAL, "unsupported rename")) as rename, \
                    patch.object(TestDirectory, "sync", side_effect=OSError(errno.EINVAL, "unsupported fsync")) as sync:
                with self.assertRaises(OSError) as error:
                    directory.rename_preflight()
                self.assertIs(original, error.exception)
                rename.assert_not_called()
                sync.assert_not_called()
        self.assertEqual(self.before, self.snapshot())
        self.assertFalse(any(x.name.startswith(".cdh-probe-") for x in self.game.iterdir()))

    def test_game_in_use_refuses_install_uninstall_and_recovery(self):
        pid = self.proc / "12345"
        pid.mkdir()
        (pid / "maps").write_text(f"100-200 r-xp 0000 00:00 1 {self.game / 'steam_api.dll'}\n")
        with patch.object(self.store, "save") as save:
            with self.assertRaisesRegex(ValueError, "Close the game"):
                self.install()
            save.assert_not_called()
        self.assertEqual(self.before, self.snapshot())
        (pid / "maps").write_text("")
        record = self.install()
        installed = self.snapshot()
        for phase in ("installed", "installing", "removing"):
            record["phase"] = phase
            self.store.save(record)
            (pid / "maps").write_text(f"100-200 r-xp 0000 00:00 1 {self.game / 'steam_api.dll'}\n")
            with patch.object(self.store, "save") as save:
                with self.assertRaisesRegex(ValueError, "Close the game"):
                    self.engine.uninstall("123")
                save.assert_not_called()
            self.assertEqual(installed, self.snapshot())
            (pid / "maps").write_text("")

    def test_steam_update_state_refuses_operations(self):
        manifest = self.library / "appmanifest_123.acf"
        for flag in (256, 1024, 65536, 131072, 262144, 524288, 1048576, 2097152, 4194304, 8388608):
            with self.subTest(flag=flag):
                self.manifest("Example")
                manifest.write_text(manifest.read_text().replace('"AppState" {', f'"AppState" {{ "StateFlags" "{flag | 4}"'))
                self.assertTrue(steam_library.find_game("123")["updating"])
                with self.assertRaisesRegex(ValueError, "Close the game"):
                    self.install()
                self.assertEqual(self.before, self.snapshot())
        self.manifest("Example")
        self.assertFalse(steam_library.find_game("123")["update_state_checked"])
        self.install()
        installed = self.snapshot()
        manifest.write_text(manifest.read_text().replace('"AppState" {', '"AppState" { "StateFlags" "1048580"'))
        with self.assertRaisesRegex(ValueError, "Close the game"):
            self.engine.uninstall("123")
        self.assertEqual(installed, self.snapshot())

    def test_not_fully_installed_manifest_refuses_with_or_without_folder(self):
        for folder in ("Example", "Missing"):
            with self.subTest(folder=folder):
                self.manifest(folder)
                manifest = self.library / "appmanifest_123.acf"
                manifest.write_text(manifest.read_text().replace('"AppState" {', '"AppState" { "StateFlags" "1"'))
                with self.assertRaisesRegex(ValueError, "not installed"):
                    steam_library.find_game("123")
                with self.assertRaisesRegex(ValueError, "not installed"):
                    self.install()
                self.assertEqual(self.before, self.snapshot())

    def test_fully_installed_manifest_with_missing_folder_refuses_clearly(self):
        self.manifest("Missing")
        manifest = self.library / "appmanifest_123.acf"
        manifest.write_text(manifest.read_text().replace('"AppState" {', '"AppState" { "StateFlags" "4"'))
        with self.assertRaisesRegex(ValueError, "not installed.*folder is missing"):
            steam_library.find_game("123")

    def test_updating_manifest_without_fully_installed_bit_keeps_busy_behavior(self):
        manifest = self.library / "appmanifest_123.acf"
        manifest.write_text(manifest.read_text().replace('"AppState" {', '"AppState" { "StateFlags" "1048576"'))
        self.assertTrue(steam_library.find_game("123")["updating"])
        with self.assertRaisesRegex(ValueError, "Close the game"):
            self.install()
        self.assertEqual(self.before, self.snapshot())

    def test_manifest_without_stateflags_keeps_legacy_behavior(self):
        game = steam_library.find_game("123")
        self.assertFalse(game["updating"])
        self.assertFalse(game["update_state_checked"])
        self.install()
        self.assertEqual([], self.engine.uninstall("123")[1])
        self.assertEqual(self.before, self.snapshot())

    def test_orphan_game_folder_is_not_an_installed_game(self):
        (self.library / "appmanifest_123.acf").unlink()
        with self.assertRaisesRegex(ValueError, "Installed Steam game not found"):
            steam_library.find_game("123")
        self.assertEqual(self.before, self.snapshot())

    def test_new_writer_after_staging_defers_rollback_until_game_is_closed(self):
        pid = self.proc / "12345"
        pid.mkdir()
        write = TestDirectory.write_new
        def writer_starts(directory, name, data):
            write(directory, name, data)
            path = (directory.path if hasattr(directory, "path")
                    else Path(os.readlink(f"/proc/self/fd/{directory.fd}")))
            if path == self.game:
                (pid / "maps").write_text(f"100-200 rw-p 0000 00:00 1 {self.game / 'steam_api.dll'}\n")
        with patch.object(TestDirectory, "write_new", writer_starts):
            with self.assertRaisesRegex(ValueError, "Close the game"):
                self.install()
        self.assertEqual("installing", self.store.load("123")["phase"])
        for name, data in self.before.items():
            self.assertEqual(data, (self.game / name).read_bytes())
        self.assertFalse(any(x.name.startswith(".cdh-q-") for x in self.game.iterdir()))
        (pid / "maps").write_text("")
        self.assertEqual([], self.engine.uninstall("123")[1])
        self.assertEqual(self.before, self.snapshot())

    def test_manual_launch_resolution_is_explicit_and_preserves_original(self):
        original = "/home/deck/wrapper %command%"
        self.install("direct", original)
        with self.assertRaises(ValueError):
            self.engine.resolve_launch("123", manual=True)
        self.engine.uninstall("123")
        self.engine.resolve_launch("123", manual=True)
        record = self.store.load("123")
        self.assertEqual("manual", record["launch_restore"])
        self.assertEqual(original, record["original_launch_options"])

    def test_legacy_removed_record_requires_launch_resolution(self):
        (self.game / "steam_api.dll").unlink()
        record = self.install("proxy", "/home/deck/wrapper %command%")
        self.engine.uninstall("123")
        record["phase"] = "removed"
        del record["quarantine"], record["launch_restore"]
        (self.settings / "123.json").write_text(json.dumps(record))
        self.assertEqual("pending", self.store.load("123")["launch_restore"])
        with self.assertRaisesRegex(ValueError, "restoration is pending"):
            self.install()

    def test_concurrent_operations_serialize(self):
        barrier = threading.Barrier(2)
        def perform():
            barrier.wait(timeout=5)
            try:
                self.install()
                return "installed"
            except ValueError:
                return "refused"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: perform(), range(2)))
        self.assertCountEqual(["installed", "refused"], results)
        self.assertEqual("installed", self.engine.status("123")[0])

    def test_shared_game_folder_refused(self):
        self.install()
        self.manifest("Example", "456")
        self.store.allow("456", True)
        before = self.tree_snapshot()
        journal = (self.settings / "123.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "folder overlaps.*123"):
            self.engine.install("456", "direct", "version", [], "")
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual(journal, (self.settings / "123.json").read_bytes())
        self.assertIsNone(self.store.load("456"))

    def test_two_games_install_and_remove_independently_in_both_modes(self):
        other = self.other_game()
        (self.game / "steam_api.dll").unlink()
        folders = {"123": self.game, "456": other}
        clean = {value: self.snapshot(folder) for value, folder in folders.items()}
        for first_mode in ("direct", "proxy"):
            for second_mode in ("direct", "proxy"):
                for removed, retained in (("123", "456"), ("456", "123")):
                    with self.subTest(first_mode=first_mode, second_mode=second_mode, removed=removed):
                        first = self.engine.install("123", first_mode, "version", ["124"], "/home/deck/a %command%")
                        first_files = self.tree_snapshot()
                        first_journal = (self.settings / "123.json").read_bytes()
                        second = self.engine.install("456", second_mode, "winmm", ["457"], "/home/deck/b %command%")
                        self.assertEqual(first_files, self.tree_snapshot())
                        self.assertEqual(first_journal, (self.settings / "123.json").read_bytes())
                        self.assertEqual("installed", self.engine.status("123")[0])
                        self.assertEqual("installed", self.engine.status("456")[0])
                        survivor = self.tree_snapshot(folders[retained])
                        journal = (self.settings / f"{retained}.json").read_bytes()
                        launch = {key: self.store.load(retained)[key] for key in (
                            "original_launch_options", "set_launch_options", "launch_restore")}
                        record, warnings = self.engine.uninstall(removed)
                        self.assertEqual([], warnings)
                        self.assertEqual("removed", record["phase"])
                        self.engine.resolve_launch(removed)
                        self.assertEqual(survivor, self.tree_snapshot(folders[retained]))
                        self.assertEqual(journal, (self.settings / f"{retained}.json").read_bytes())
                        self.assertEqual(launch, {key: self.store.load(retained)[key] for key in launch})
                        self.assertEqual("installed", self.engine.status(retained)[0])
                        self.assertEqual([], self.engine.uninstall(retained)[1])
                        self.engine.resolve_launch(retained)
                        for value, folder in folders.items():
                            self.assertEqual(clean[value], self.snapshot(folder))
                        self.assertEqual(["124"], first["enabled_dlc_ids"])
                        self.assertEqual(["457"], second["enabled_dlc_ids"])

    def test_same_app_active_phases_refuse_without_mutation(self):
        record = self.install()
        for phase in ("installing", "installed", "removing"):
            with self.subTest(phase=phase):
                record["phase"] = phase
                self.store.save(record)
                before = self.tree_snapshot()
                journal = (self.settings / "123.json").read_bytes()
                with self.assertRaisesRegex(ValueError, "this game's installation first"):
                    self.install()
                self.assertEqual(before, self.tree_snapshot())
                self.assertEqual(journal, (self.settings / "123.json").read_bytes())

    def test_concurrent_different_game_installs_succeed(self):
        other = self.other_game()
        barrier = threading.Barrier(2)
        def perform(value):
            barrier.wait(timeout=5)
            return self.engine.install(value, "direct", "version", [], "")["phase"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(["installed", "installed"], list(pool.map(perform, ("123", "456"))))
        for value in ("123", "456"):
            self.assertEqual("installed", self.engine.status(value)[0])

    def test_allowlisting_one_active_game_does_not_authorize_another(self):
        other = self.other_game()
        (self.settings / "allowlist.json").write_text('["123"]')
        self.install()
        before, other_before = self.tree_snapshot(), self.tree_snapshot(other)
        with self.assertRaisesRegex(ValueError, "not on the owner allowlist"):
            self.engine.install("456", "direct", "version", [], "")
        with self.assertRaisesRegex(ValueError, "Confirm offline single-player"):
            self.store.allow("456", False)
        self.assertEqual(["123"], self.store.allowlist())
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual(other_before, self.tree_snapshot(other))

    def test_nested_and_containing_game_folders_refused(self):
        self.install()
        child = self.game / "Child"
        child.mkdir()
        (child / "steam_api64.dll").write_bytes(b"child original DLL")
        self.store.allow("456", True)
        find = steam_library.find_game
        for folder in (child, self.game.parent):
            with self.subTest(folder=folder), patch.object(self.engine, "find_game", side_effect=lambda value: (
                    {"app_id": "456", "game_directory": str(folder)} if value == "456" else find(value))):
                before = self.tree_snapshot(self.game.parent)
                with self.assertRaisesRegex(ValueError, "folder overlaps.*123"):
                    self.engine.install("456", "direct", "version", [], "")
                self.assertEqual(before, self.tree_snapshot(self.game.parent))
                self.assertIsNone(self.store.load("456"))

    def test_same_root_with_disjoint_api_folders_refused(self):
        first = self.game / "First"
        first.mkdir()
        for name in install_state.APIS:
            (self.game / name).rename(first / name)
        self.install()
        second = self.game / "Second"
        second.mkdir()
        (second / "steam_api64.dll").write_bytes(b"second original DLL")
        self.manifest("Example", "456")
        self.store.allow("456", True)
        self.engine.launch_executables = lambda value: ["Second/game.exe"]
        before = self.tree_snapshot()
        with self.assertRaisesRegex(ValueError, "folder overlaps.*123"):
            self.engine.install("456", "direct", "version", [], "")
        self.assertEqual(before, self.tree_snapshot())
        self.assertIsNone(self.store.load("456"))

    def test_shared_file_inodes_refused_across_distinct_folders(self):
        other = self.other_game()
        (self.game / "steam_api.dll").unlink()
        for mode in ("direct", "proxy"):
            with self.subTest(mode=mode):
                self.install(mode)
                source = self.game / ("steam_api64_o.dll" if mode == "direct" else "steam_api64.dll")
                target = other / "steam_api64.dll"
                target.unlink()
                os.link(source, target)
                before, other_before = self.tree_snapshot(), self.tree_snapshot(other)
                journal = (self.settings / "123.json").read_bytes()
                with self.assertRaisesRegex(ValueError, "files overlap.*123"):
                    self.engine.install("456", "direct", "version", [], "")
                self.assertEqual(before, self.tree_snapshot())
                self.assertEqual(other_before, self.tree_snapshot(other))
                self.assertEqual(journal, (self.settings / "123.json").read_bytes())
                self.assertIsNone(self.store.load("456"))
                self.assertEqual([], self.engine.uninstall("123")[1])
                self.engine.resolve_launch("123")
                target.unlink()
                target.write_bytes(b"other original DLL")

    def test_missing_manifest_keeps_recorded_game_root(self):
        api = self.game / "Engine" / "Binaries"
        api.mkdir(parents=True)
        for name in install_state.APIS:
            (self.game / name).rename(api / name)
        self.install()
        (self.library / "appmanifest_123.acf").unlink()
        other = self.game / "ChildLibrary" / "steamapps" / "common" / "Inner"
        other.mkdir(parents=True)
        (other / "steam_api64.dll").write_bytes(b"other original DLL")
        self.store.allow("456", True)
        find = self.engine.find_game
        before = self.tree_snapshot()
        journal = (self.settings / "123.json").read_bytes()
        with patch.object(self.engine, "find_game", side_effect=lambda value: (
                {"app_id": value, "game_directory": str(other)} if value == "456" else find(value))):
            with self.assertRaisesRegex(ValueError, "folder overlaps.*123"):
                self.engine.install("456", "direct", "version", [], "")
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual(journal, (self.settings / "123.json").read_bytes())
        self.assertIsNone(self.store.load("456"))

    def test_game_root_is_persisted_and_schema_validated(self):
        api = self.game / "Engine" / "Binaries"
        api.mkdir(parents=True)
        for name in install_state.APIS:
            (self.game / name).rename(api / name)
        record = self.install()
        self.assertEqual(str(self.game), record["game_directory"])
        self.assertEqual(str(api), record["game_dir"])
        self.assertEqual(str(self.game), self.store.load("123")["game_directory"])
        for bad in (None, 123, "relative", str(self.game / ".."), str(self.base / "elsewhere")):
            with self.subTest(bad=bad):
                altered = {**record, "game_directory": bad}
                with self.assertRaises(ValueError):
                    install_state.validate(altered, "123")

    def legacy_install(self):
        api = self.game / "Engine" / "Binaries"
        api.mkdir(parents=True)
        for name in install_state.APIS:
            (self.game / name).rename(api / name)
        record = self.install()
        record.pop("game_directory", None)
        (self.settings / "123.json").write_text(json.dumps(record))
        return record

    def test_legacy_missing_manifest_refuses_install_inside_unknown_game_root(self):
        self.legacy_install()
        (self.library / "appmanifest_123.acf").unlink()
        other = self.game / "ChildLibrary" / "steamapps" / "common" / "Inner"
        other.mkdir(parents=True)
        (other / "steam_api64.dll").write_bytes(b"other original DLL")
        self.store.allow("456", True)
        find = self.engine.find_game
        before = self.tree_snapshot()
        journal = (self.settings / "123.json").read_bytes()
        with patch.object(self.engine, "find_game", side_effect=lambda value: (
                {"app_id": value, "game_directory": str(other)} if value == "456" else find(value))):
            with self.assertRaisesRegex(ValueError, "Cannot confirm the folder of app 123"):
                self.engine.install("456", "direct", "version", [], "")
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual(journal, (self.settings / "123.json").read_bytes())
        self.assertIsNone(self.store.load("456"))

    def test_legacy_present_manifest_migrates_before_nested_install_refusal(self):
        original = self.legacy_install()
        other = self.game / "ChildLibrary" / "steamapps" / "common" / "Inner"
        other.mkdir(parents=True)
        (other / "steam_api64.dll").write_bytes(b"other original DLL")
        self.store.allow("456", True)
        find = self.engine.find_game
        before = self.tree_snapshot()
        with patch.object(self.engine, "find_game", side_effect=lambda value: (
                {"app_id": value, "game_directory": str(other)} if value == "456" else find(value))):
            with self.assertRaisesRegex(ValueError, "folder overlaps.*123"):
                self.engine.install("456", "direct", "version", [], "")
        migrated = json.loads((self.settings / "123.json").read_text())
        self.assertEqual({**original, "game_directory": str(self.game)}, migrated)
        self.assertEqual(migrated, install_state.validate(migrated, "123"))
        self.assertEqual(before, self.tree_snapshot())
        self.assertIsNone(self.store.load("456"))

    def test_legacy_present_manifest_does_not_block_unrelated_install(self):
        original = self.legacy_install()
        other = self.other_game()
        self.engine.install("456", "direct", "version", [], "")
        self.assertEqual("installed", self.engine.status("456")[0])
        self.assertTrue((other / install_state.CONFIG).exists())
        self.assertEqual({**original, "game_directory": str(self.game)}, self.store.load("123"))

    def test_legacy_active_reads_migrate_only_root_and_removed_reads_do_not_write(self):
        original = self.legacy_install()
        for phase in ("installing", "installed", "removing", "removed"):
            with self.subTest(phase=phase):
                legacy = {**original, "phase": phase}
                (self.settings / "123.json").write_text(json.dumps(legacy))
                before = (self.settings / "123.json").read_bytes()
                with patch.object(self.store, "_replace", wraps=self.store._replace) as save:
                    loaded = self.store.load("123")
                if phase == "removed":
                    save.assert_not_called()
                    self.assertEqual(before, (self.settings / "123.json").read_bytes())
                    self.assertEqual(legacy, loaded)
                else:
                    expected = {**legacy, "game_directory": str(self.game)}
                    save.assert_called_once_with(expected)
                    self.assertEqual(expected, loaded)
                    self.assertEqual(expected, json.loads((self.settings / "123.json").read_text()))
                    self.assertEqual(expected, install_state.validate(loaded, "123"))

    def test_legacy_status_migrates_root(self):
        original = self.legacy_install()
        status, migrated = self.engine.status("123")
        self.assertEqual("installed", status)
        self.assertEqual({**original, "game_directory": str(self.game)}, migrated)
        self.assertEqual(migrated, json.loads((self.settings / "123.json").read_text()))

    def test_legacy_migration_does_not_persist_validation_defaults(self):
        original = self.legacy_install()
        for name in ("quarantine", "launch_restore", "foreign_proxies"):
            original.pop(name)
        (self.settings / "123.json").write_text(json.dumps(original))
        migrated = self.store.load("123")
        persisted = json.loads((self.settings / "123.json").read_text())
        self.assertEqual({**original, "game_directory": str(self.game)}, persisted)
        self.assertEqual(install_state.validate(persisted, "123"), migrated)

    def test_legacy_removed_journal_does_not_block_install_or_migrate(self):
        self.install()
        removed, warnings = self.engine.uninstall("123")
        self.assertEqual([], warnings)
        removed.pop("game_directory")
        (self.settings / "123.json").write_text(json.dumps(removed))
        journal = (self.settings / "123.json").read_bytes()
        self.other_game()
        self.engine.install("456", "direct", "version", [], "")
        self.assertEqual("installed", self.engine.status("456")[0])
        self.assertEqual(journal, (self.settings / "123.json").read_bytes())

    def test_legacy_migrated_root_survives_manifest_loss(self):
        original = self.legacy_install()
        self.assertEqual({**original, "game_directory": str(self.game)}, self.store.load("123"))
        (self.library / "appmanifest_123.acf").unlink()
        other = self.game / "ChildLibrary" / "steamapps" / "common" / "Inner"
        other.mkdir(parents=True)
        (other / "steam_api64.dll").write_bytes(b"other original DLL")
        self.store.allow("456", True)
        find = self.engine.find_game
        journal = (self.settings / "123.json").read_bytes()
        before = self.tree_snapshot()
        with patch.object(self.engine, "find_game", side_effect=lambda value: (
                {"app_id": value, "game_directory": str(other)} if value == "456" else find(value))):
            with self.assertRaisesRegex(ValueError, "folder overlaps.*123"):
                self.engine.install("456", "direct", "version", [], "")
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual(journal, (self.settings / "123.json").read_bytes())

    def test_legacy_unknown_root_refuses_unrelated_install_without_mutation(self):
        original = self.legacy_install()
        other = self.other_game()
        for failure in (ValueError("missing manifest"), PermissionError("unreadable manifest"),
                        {"game_directory": str(other)}):
            with self.subTest(failure=failure):
                (self.settings / "123.json").write_text(json.dumps(original))
                before = self.tree_snapshot(self.game.parent)
                journal = (self.settings / "123.json").read_bytes()
                find = steam_library.find_game
                def lookup(value, **kwargs):
                    if value != "123":
                        return find(value, **kwargs)
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                with patch.object(steam_library, "find_game", side_effect=lookup), patch.object(
                        self.engine, "find_game", side_effect=lookup):
                    self.assertEqual(original, self.store.load("123"))
                    with self.assertRaisesRegex(ValueError, "Cannot confirm the folder of app 123"):
                        self.engine.install("456", "direct", "version", [], "")
                self.assertEqual(before, self.tree_snapshot(self.game.parent))
                self.assertEqual(journal, (self.settings / "123.json").read_bytes())
                self.assertIsNone(self.store.load("456"))

    def test_legacy_migration_write_failure_keeps_load_and_status_available(self):
        original = self.legacy_install()
        journal = (self.settings / "123.json").read_bytes()
        before = self.tree_snapshot()
        for error in (OSError("replace failed"), ValueError("replace refused")):
            with self.subTest(error=error), patch.object(TestDirectory, "atomic_json", side_effect=error):
                self.assertEqual(original, self.store.load("123"))
                self.assertEqual(("installed", original), self.engine.status("123"))
                self.assertEqual(journal, (self.settings / "123.json").read_bytes())
                self.assertEqual(before, self.tree_snapshot())

    def test_legacy_migration_write_failure_allows_uninstall_and_recovery(self):
        original = self.legacy_install()
        api = Path(original["game_dir"])
        installed = self.snapshot(api)
        replace = self.store._replace
        for phase in ("installed", "installing", "removing"):
            for error in (OSError("replace failed"), ValueError("replace refused")):
                with self.subTest(phase=phase, error=error):
                    for name, data in installed.items():
                        (api / name).write_bytes(data)
                    legacy = {**original, "phase": phase}
                    (self.settings / "123.json").write_text(json.dumps(legacy))
                    def fail_migration(record):
                        if "game_directory" in record:
                            raise error
                        return replace(record)
                    with patch.object(self.store, "_replace", side_effect=fail_migration):
                        removed, warnings = self.engine.uninstall("123")
                    self.assertEqual([], warnings)
                    self.assertEqual("removed", removed["phase"])
                    self.assertNotIn("game_directory", removed)
                    self.assertEqual(removed, self.store.load("123"))
                    self.assertEqual(self.before, {**self.snapshot(), **self.snapshot(api)})

    def test_legacy_migration_write_failure_refuses_other_install(self):
        original = self.legacy_install()
        self.other_game()
        journal = (self.settings / "123.json").read_bytes()
        before = self.tree_snapshot(self.game.parent)
        for error in (OSError("replace failed"), ValueError("replace refused")):
            with self.subTest(error=error), patch.object(TestDirectory, "atomic_json", side_effect=error):
                self.assertEqual(original, self.store.load("123"))
                with self.assertRaisesRegex(ValueError, "Cannot confirm the folder of app 123"):
                    self.engine.install("456", "direct", "version", [], "")
                self.assertEqual(journal, (self.settings / "123.json").read_bytes())
                self.assertEqual(before, self.tree_snapshot(self.game.parent))
                self.assertIsNone(self.store.load("456"))

    def test_legacy_duplicate_manifests_never_migrate_narrower_root(self):
        nested_library = self.game / "ChildLibrary" / "steamapps"
        nested = nested_library / "common" / "Inner"
        api = nested / "Engine" / "Binaries"
        api.mkdir(parents=True)
        for name in install_state.APIS:
            (self.game / name).rename(api / name)
        original = self.install()
        original.pop("game_directory")
        (self.settings / "123.json").write_text(json.dumps(original))
        duplicate = nested_library / "appmanifest_123.acf"
        duplicate.write_text('"AppState" { "appid" "123" "installdir" "Inner" }')
        other = self.game / "OtherBranch"
        other.mkdir()
        (other / "steam_api64.dll").write_bytes(b"other original DLL")
        self.store.allow("456", True)
        journal = (self.settings / "123.json").read_bytes()
        before = self.tree_snapshot()
        find = self.engine.find_game
        with patch.object(steam_library, "library_folders", return_value=[str(nested_library), str(self.library)]), patch.object(
                self.engine, "find_game", side_effect=lambda value: (
                    {"app_id": value, "game_directory": str(other)} if value == "456" else find(value))):
            self.assertEqual(original, self.store.load("123"))
            with self.assertRaisesRegex(ValueError, "Cannot confirm the folder of app 123"):
                self.engine.install("456", "direct", "version", [], "")
            self.assertEqual(journal, (self.settings / "123.json").read_bytes())
            self.assertEqual(before, self.tree_snapshot())
            self.assertIsNone(self.store.load("456"))
            duplicate.unlink()
            (self.library / "appmanifest_123.acf").unlink()
            before = self.tree_snapshot()
            with self.assertRaisesRegex(ValueError, "Cannot confirm the folder of app 123"):
                self.engine.install("456", "direct", "version", [], "")
            self.assertEqual(journal, (self.settings / "123.json").read_bytes())
            self.assertEqual(before, self.tree_snapshot())
            self.assertIsNone(self.store.load("456"))

    def test_legacy_journal_missing_manifest_still_loads(self):
        original = self.legacy_install()
        (self.library / "appmanifest_123.acf").unlink()
        legacy = self.store.load("123")
        self.assertEqual(original, legacy)
        with self.assertRaisesRegex(ValueError, "folder overlaps.*123"):
            self.engine._check_folders({"game_directory": os.path.join(legacy["game_dir"], "Child")}, [legacy])

    def test_legacy_uninstall_and_recovery_preserve_missing_manifest_safety(self):
        original = self.legacy_install()
        installed = self.tree_snapshot()
        for phase in ("installed", "installing", "removing"):
            with self.subTest(phase=phase):
                legacy = {**original, "phase": phase}
                (self.settings / "123.json").write_text(json.dumps(legacy))
                (self.library / "appmanifest_123.acf").unlink()
                journal = (self.settings / "123.json").read_bytes()
                with self.assertRaisesRegex(ValueError, "Installed Steam game not found"):
                    self.engine.uninstall("123")
                self.assertEqual(journal, (self.settings / "123.json").read_bytes())
                self.assertEqual(installed, self.tree_snapshot())
                self.manifest("Example")
        record, warnings = self.engine.uninstall("123")
        self.assertEqual([], warnings)
        self.assertEqual("removed", record["phase"])
        self.assertEqual(str(self.game), record["game_directory"])
        self.assertEqual(self.before, {**self.snapshot(), **self.snapshot(Path(original["game_dir"]))})

    def test_legacy_unknown_root_does_not_block_other_game_uninstall_or_recovery(self):
        legacy = self.legacy_install()
        other = self.other_game()
        clean = self.snapshot(other)
        record = self.engine.install("456", "proxy", "winmm", [], "")
        (self.settings / "123.json").write_text(json.dumps(legacy))
        (self.library / "appmanifest_123.acf").unlink()
        journal = (self.settings / "123.json").read_bytes()
        before = self.tree_snapshot()
        for phase in ("installed", "installing", "removing"):
            with self.subTest(phase=phase):
                record["phase"] = phase
                self.store.save(record)
                result, warnings = self.engine.uninstall("456")
                self.assertEqual([], warnings)
                self.assertEqual("removed", result["phase"])
                self.engine.resolve_launch("456")
                self.assertEqual(clean, self.snapshot(other))
                self.assertEqual(journal, (self.settings / "123.json").read_bytes())
                self.assertEqual(before, self.tree_snapshot())

    def test_symlink_modeled_game_aliases_and_physical_ancestry_refused(self):
        api = self.game / "First"
        api.mkdir()
        for name in install_state.APIS:
            (self.game / name).rename(api / name)
        record = self.install()
        (self.game / "Second").mkdir()
        (self.game / "Second" / "steam_api64.dll").write_bytes(b"second original DLL")
        alias = self.base / "Alias"
        alias.symlink_to(self.game, target_is_directory=True)
        child_alias = self.base / "ChildAlias"
        child_alias.symlink_to(self.game / "Second", target_is_directory=True)
        parent_alias = self.base / "ParentAlias"
        parent_alias.symlink_to(self.game.parent, target_is_directory=True)
        before = self.tree_snapshot()
        # Model mount aliases only for the read-only guard. Mutation still rejects symlinks.
        with patch.object(smokeapi, "Directory", side_effect=lambda path: TestDirectory(os.path.realpath(path))):
            for candidate in (alias, child_alias, parent_alias):
                with self.subTest(candidate=candidate.name):
                    with self.assertRaisesRegex(ValueError, "folder overlaps.*123"):
                        self.engine._check_folders({"game_directory": str(candidate)}, [record])
        self.assertEqual(before, self.tree_snapshot())

    def test_case_insensitive_game_root_alias_with_disjoint_api_folders_refused(self):
        api = self.game / "First"
        api.mkdir()
        for name in install_state.APIS:
            (self.game / name).rename(api / name)
        record = self.install()
        (self.game / "Second").mkdir()
        (self.game / "Second" / "steam_api64.dll").write_bytes(b"second original DLL")
        alias = str(self.game.parent / "eXAMPLE")
        def directory(path):
            if path == alias or path.startswith(alias + os.sep):
                path = str(self.game) + path[len(alias):]
            return TestDirectory(path)
        def lexical(first, second):
            first, second = (path.replace("\\", "/") for path in (first, second))
            return posixpath.commonpath([first, second]) in (first, second)
        before = self.tree_snapshot()
        with patch.object(smokeapi, "Directory", side_effect=directory), patch.object(
                smokeapi, "folders_overlap", side_effect=lexical):
            with self.assertRaisesRegex(ValueError, "folder overlaps.*123"):
                self.engine._check_folders({"game_directory": alias}, [record])
        self.assertEqual(before, self.tree_snapshot())

    def test_physical_api_folder_aliases_and_ancestry_refused(self):
        api = self.game / "Engine" / "Binaries"
        api.mkdir(parents=True)
        for name in install_state.APIS:
            (self.game / name).rename(api / name)
        record = self.install()
        other = self.other_game()
        before, other_before = self.tree_snapshot(), self.tree_snapshot(other)
        alias = other / "Plugins"
        # Model a bind-mounted API folder while keeping the game roots distinct.
        def directory(path):
            return TestDirectory(str(api) if path == str(alias) else path)
        with patch.object(smokeapi, "Directory", side_effect=directory):
            self.engine._check_folders({"game_directory": str(other)}, [record])
            with self.assertRaisesRegex(ValueError, "folder overlaps.*123"):
                self.engine._check_folders({"game_directory": str(other)}, [record], str(alias))
        for destination in (other, other / "Child"):
            destination.mkdir(exist_ok=True)
            def directory(path):
                return TestDirectory(str(destination) if path == str(api) else path)
            with self.subTest(destination=destination.name), patch.object(
                    smokeapi, "Directory", side_effect=directory):
                with self.assertRaisesRegex(ValueError, "folder overlaps.*123"):
                    self.engine._check_folders({"game_directory": str(other)}, [record])
        (other / "Child").rmdir()
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual(other_before, self.tree_snapshot(other))

    def test_case_variant_recorded_backup_shared_inode_refused(self):
        other = self.other_game()
        self.install()
        backup = self.game / "STEAM_API64_O.DLL"
        (self.game / "steam_api64_o.dll").rename(backup)
        target = other / "steam_api64.dll"
        target.unlink()
        os.link(backup, target)
        before, other_before = self.tree_snapshot(), self.tree_snapshot(other)
        journal = (self.settings / "123.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "files overlap.*123"):
            self.engine.install("456", "direct", "version", [], "")
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual(other_before, self.tree_snapshot(other))
        self.assertEqual(journal, (self.settings / "123.json").read_bytes())
        self.assertIsNone(self.store.load("456"))

    def test_interrupted_install_recovers_while_other_game_stays_active(self):
        other = self.other_game()
        (self.game / "steam_api.dll").unlink()
        self.engine.install("456", "proxy", "winmm", ["457"], "/home/deck/b %command%")
        before, journal = self.tree_snapshot(other), (self.settings / "456.json").read_bytes()
        clean = self.snapshot()
        rename = TestDirectory.rename
        for mode in ("direct", "proxy"):
            count = 0
            def crash(directory, *args):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("simulated interrupted commit")
                return rename(directory, *args)
            with self.subTest(mode=mode):
                with patch.object(TestDirectory, "rename", crash), patch.object(
                        self.engine, "_remove", side_effect=OSError("simulated process exit before rollback")):
                    with self.assertRaisesRegex(OSError, "simulated process exit"):
                        self.install(mode, "/home/deck/a %command%")
                restarted = smokeapi.Installer(install_state.Store(str(self.settings)), steam_library.find_game)
                self.assertIn("interrupted", restarted.status("123")[0])
                self.assertEqual("installed", restarted.status("456")[0])
                record, warnings = restarted.uninstall("123")
                self.assertEqual([], warnings)
                self.assertEqual("removed", record["phase"])
                restarted.resolve_launch("123", manual=True)
                self.assertEqual(clean, self.snapshot())
                self.assertEqual(before, self.tree_snapshot(other))
                self.assertEqual(journal, (self.settings / "456.json").read_bytes())

    def test_other_game_pending_launch_restore_does_not_block_install(self):
        other = self.other_game()
        (self.game / "steam_api.dll").unlink()
        for mode in ("direct", "proxy"):
            with self.subTest(mode=mode):
                self.install("proxy", "/home/deck/a %command%")
                config = (self.game / install_state.CONFIG).read_bytes()
                (self.game / install_state.CONFIG).write_bytes(b"owner config")
                record, warnings = self.engine.uninstall("123")
                self.assertTrue(warnings)
                self.assertEqual("removing", record["phase"])
                self.assertEqual("pending", record["launch_restore"])
                before, journal = self.tree_snapshot(), (self.settings / "123.json").read_bytes()
                self.engine.install("456", mode, "winmm", [], "/home/deck/b %command%")
                self.assertEqual("installed", self.engine.status("456")[0])
                self.assertEqual([], self.engine.uninstall("456")[1])
                self.engine.resolve_launch("456")
                self.assertEqual(before, self.tree_snapshot())
                self.assertEqual(journal, (self.settings / "123.json").read_bytes())
                (self.game / install_state.CONFIG).write_bytes(config)
                self.assertEqual([], self.engine.uninstall("123")[1])
                self.assertEqual("pending", self.store.load("123")["launch_restore"])
                self.engine.install("456", mode, "winmm", [], "")
                self.assertEqual([], self.engine.uninstall("456")[1])
                self.engine.resolve_launch("456")
                self.engine.resolve_launch("123")

    def test_unrelated_malformed_journal_does_not_block_scoped_operations(self):
        other = self.other_game()
        self.engine.install("456", "proxy", "winmm", [], "")
        (self.settings / "123.json").write_bytes(b"{invalid")
        before = self.tree_snapshot()
        self.store.allow("456", True)
        self.assertEqual("installed", self.engine.status("456")[0])
        self.assertEqual([], self.engine.uninstall("456")[1])
        self.engine.resolve_launch("456")
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual(b"{invalid", (self.settings / "123.json").read_bytes())
        with self.assertRaises(ValueError):
            self.engine.install("456", "direct", "version", [], "")

    def test_missing_other_manifest_does_not_block_independent_install(self):
        other = self.other_game()
        self.install()
        (self.library / "appmanifest_123.acf").unlink()
        before, journal = self.tree_snapshot(), (self.settings / "123.json").read_bytes()
        self.engine.install("456", "direct", "version", [], "")
        self.assertEqual("installed", self.engine.status("456")[0])
        self.assertEqual([], self.engine.uninstall("456")[1])
        self.engine.resolve_launch("456")
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual(journal, (self.settings / "123.json").read_bytes())

    def test_interrupted_settings_write_preserved_during_other_game_recovery(self):
        other = self.other_game()
        self.install()
        self.engine.install("456", "proxy", "winmm", [], "")
        before, journal = self.tree_snapshot(), (self.settings / "123.json").read_bytes()
        stage = self.settings / (".cdh-" + "a" * 32)
        stage.write_bytes(b"interrupted settings write")
        self.assertEqual("installed", self.engine.status("456")[0])
        self.assertEqual([], self.engine.uninstall("456")[1])
        self.engine.resolve_launch("456")
        self.store.allow("456", True)
        with self.assertRaisesRegex(ValueError, "Interrupted settings write"):
            self.engine.install("456", "direct", "version", [], "")
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual(journal, (self.settings / "123.json").read_bytes())
        self.assertEqual(b"interrupted settings write", stage.read_bytes())

    def test_complex_launch_options_refused(self):
        for original in ("WINEDLLOVERRIDES='version=b' %command%", "echo hi; %command%", '"/path with spaces" %command%'):
            with self.subTest(original=original), self.assertRaises(ValueError):
                self.install("proxy", original)
        self.assertEqual(self.before, self.snapshot())

    def test_complex_launch_options_manual_setup_requires_owner_choice(self):
        (self.game / "steam_api.dll").unlink()
        original = "WINEDLLOVERRIDES='version=b' %command%"
        record = self.engine.install("123", "proxy", "version", [], original, manual_launch=True)
        self.assertIsNone(record["set_launch_options"])
        self.assertEqual(original, record["original_launch_options"])
        self.assertEqual([], self.engine.uninstall("123")[1])

    def unreal_layout(self, *arches):
        for name in install_state.APIS:
            (self.game / name).unlink()
        folders = []
        for arch in arches:
            folder = self.game / "Engine" / "Binaries" / "ThirdParty" / "Steamworks" / "Steamv139" / arch
            folder.mkdir(parents=True)
            (folder / "steam_api64.dll").write_bytes(b"original nested DLL")
            folders.append(folder)
        return folders

    def test_unreal_nested_steam_api_installs_beside_it_and_removes_byte_exact(self):
        folder, = self.unreal_layout("Win64")
        before = {x.name: x.read_bytes() for x in folder.iterdir()}
        top = self.snapshot()
        record = self.install()
        self.assertEqual(str(folder), record["game_dir"])
        self.assertEqual({"steam_api64.dll", "steam_api64_o.dll", install_state.CONFIG}, {x.name for x in folder.iterdir()})
        self.assertEqual(top, self.snapshot())
        self.assertEqual("installed", self.engine.status("123")[0])
        self.assertEqual([], self.engine.uninstall("123")[1])
        self.assertEqual(before, {x.name: x.read_bytes() for x in folder.iterdir()})

    def test_deep_plugin_steam_api_is_found(self):
        # FleshMadeFear (UE5) ships it ten folders down, inside a marketplace plugin.
        for name in install_state.APIS:
            (self.game / name).unlink()
        folder = self.game.joinpath("Engine", "Plugins", "Marketplace", "SteamCorePro_5.4", "Source", "ThirdParty",
                                    "SteamLibrary", "redistributable_bin", "win64")
        folder.mkdir(parents=True)
        (folder / "steam_api64.dll").write_bytes(b"original deep DLL")
        self.assertEqual(str(folder), smokeapi.api_directory(str(self.game)))

    def test_unreal_nested_steam_api_refuses_proxy_and_ambiguous_folders(self):
        win64, = self.unreal_layout("Win64")
        with self.assertRaisesRegex(ValueError, "Proxy mode"):
            self.install("proxy")
        win32 = win64.parent / "Win32"
        win32.mkdir()
        (win32 / "steam_api.dll").write_bytes(b"original nested 32-bit DLL")
        with self.assertRaisesRegex(ValueError, "Several Steam API DLL folders"):
            self.install()
        self.assertEqual({"steam_api64.dll"}, {x.name for x in win64.iterdir()})

    def test_reserved_names_refused_in_any_letter_case(self):
        def exact_exists(directory, name):
            return name in directory.names()
        for mode in ("direct", "proxy"):
            for name in ("Steam_API_o.dll", "Steam_API64_o.dll", install_state.CONFIG.upper(), ".DLC_UNLOCKER_PROXY.JSON"):
                with self.subTest(mode=mode, name=name), patch.object(TestDirectory, "exists", exact_exists):
                    file = self.game / name
                    file.write_bytes(b"foreign injector")
                    before = self.tree_snapshot()
                    with self.assertRaisesRegex(ValueError, "Foreign file already exists"):
                        self.install(mode)
                    self.assertEqual(before, self.tree_snapshot())
                    file.unlink()

    def test_proxy_occupied_name_refused_in_any_letter_case(self):
        (self.game / "steam_api.dll").unlink()
        for proxy in install_state.PROXIES:
            for name in (proxy + ".dll", proxy.upper() + ".DLL", proxy.capitalize() + ".dll"):
                with self.subTest(proxy=proxy, name=name), patch.object(TestDirectory, "exists", lambda directory, name: name in directory.names()):
                    file = self.game / name
                    file.write_bytes(b"foreign injector")
                    before = self.tree_snapshot()
                    with self.assertRaisesRegex(ValueError, "Foreign file already exists"):
                        self.engine.install("123", "proxy", proxy, [], "")
                    self.assertEqual(before, self.tree_snapshot())
                    file.unlink()

    def test_direct_foreign_proxy_bytes_and_inode_preserved(self):
        file = self.game / "WinMM.dll"
        file.write_bytes(b"owner OptiScaler")
        before = self.tree_snapshot()
        record = self.install()
        self.assertEqual({"WinMM.dll": safe_fs.digest(b"owner OptiScaler")}, record["foreign_proxies"])
        self.assertNotIn("WinMM.dll", record["created"])
        self.assertNotIn("WinMM.dll", record["stages"])
        self.assertEqual(before["WinMM.dll"], self.tree_snapshot()["WinMM.dll"])
        self.assertEqual([], self.engine.uninstall("123")[1])
        self.assertEqual(before, self.tree_snapshot())

    def test_direct_changed_foreign_proxy_warns_and_preserves_identity(self):
        self.assert_foreign_proxy_change_is_advisory(deleted=False)

    def test_direct_deleted_foreign_proxy_warns_without_blocking_removal(self):
        self.assert_foreign_proxy_change_is_advisory(deleted=True)

    def assert_foreign_proxy_change_is_advisory(self, deleted):
        file = self.game / "WinMM.dll"
        file.write_bytes(b"owner OptiScaler")
        self.install()
        if deleted:
            file.unlink()
        else:
            file.write_bytes(b"owner OptiScaler update")
        changed = self.tree_snapshot().get("WinMM.dll")
        record, warnings = self.engine.uninstall("123")
        self.assertEqual(["Foreign proxy changed since install (WinMM.dll); left alone"], warnings)
        self.assertEqual("removed", record["phase"])
        self.assertEqual("acknowledged", record["launch_restore"])
        self.assertEqual(changed, self.tree_snapshot().get("WinMM.dll"))
        self.assertEqual(self.before, {name: data for name, data in self.snapshot().items() if name != "WinMM.dll"})

        restarted = smokeapi.Installer(install_state.Store(str(self.settings)), steam_library.find_game)
        recovered, warnings = restarted.uninstall("123")
        self.assertEqual("removed", recovered["phase"])
        self.assertEqual([], warnings)
        other = self.game.parent / "Other"
        other.mkdir()
        (other / "steam_api64.dll").write_bytes(b"other original DLL")
        self.manifest("Other", "456")
        self.store.allow("456", True)
        self.assertEqual("installed", restarted.install("456", "direct", "version", [], "")["phase"])
        self.assertEqual("removed", restarted.uninstall("123")[0]["phase"])
        self.assertEqual("installed", restarted.status("456")[0])
        self.assertEqual(changed, self.tree_snapshot().get("WinMM.dll"))
        self.assertEqual(self.before, {name: data for name, data in self.snapshot().items() if name != "WinMM.dll"})

    def test_real_cleanup_warning_still_blocks_removal_with_changed_foreign_proxy(self):
        file = self.game / "WinMM.dll"
        file.write_bytes(b"owner OptiScaler")
        self.install()
        file.write_bytes(b"owner OptiScaler update")
        (self.game / install_state.CONFIG).write_bytes(b"owner config")
        changed = self.tree_snapshot()
        for _ in range(2):
            record, warnings = self.engine.uninstall("123")
            self.assertEqual("removing", record["phase"])
            self.assertEqual("removing", self.store.load("123")["phase"])
            self.assertIn("Foreign proxy changed since install (WinMM.dll); left alone", warnings)
            self.assertIn(smokeapi.CHANGED + f" ({install_state.CONFIG}); file left alone", warnings)
            for name in ("WinMM.dll", install_state.CONFIG):
                self.assertEqual(changed[name], self.tree_snapshot()[name])
            for name in install_state.APIS:
                self.assertEqual(self.before[name], (self.game / name).read_bytes())

    def test_changed_foreign_proxy_does_not_turn_install_rollback_into_failure(self):
        file = self.game / "WinMM.dll"
        file.write_bytes(b"owner OptiScaler")
        rename = TestDirectory.rename
        count = 0
        failure = OSError("simulated rename failure")
        def fail(directory, source, target, expected):
            nonlocal count
            count += 1
            if count == 3:
                file.write_bytes(b"owner OptiScaler update")
                raise failure
            return rename(directory, source, target, expected)
        with patch.object(TestDirectory, "rename", fail):
            with self.assertRaises(OSError) as error:
                self.install()
        self.assertIs(failure, error.exception)
        self.assertEqual("removed", self.store.load("123")["phase"])
        self.assertEqual(b"owner OptiScaler update", file.read_bytes())
        self.assertEqual(self.before, {name: data for name, data in self.snapshot().items() if name != "WinMM.dll"})

    def test_foreign_proxy_schema_rejects_unsafe_names_and_hashes(self):
        record = self.install()
        for foreign in ({"../winmm.dll": "0" * 64}, {"dxgi.dll": "0" * 64}, {"winmm.dll": "bad"}, []):
            with self.subTest(foreign=foreign):
                altered = {**record, "foreign_proxies": foreign}
                with self.assertRaises(ValueError):
                    install_state.validate(altered, "123")

    def test_available_proxies_omit_case_aliases_and_allow_other_names(self):
        (self.game / "steam_api.dll").unlink()
        (self.game / "WinMM.dll").write_bytes(b"owner OptiScaler")
        self.assertEqual(["version", "winhttp"], smokeapi.available_proxies(str(self.game)))
        before = self.tree_snapshot()
        self.install("proxy")
        self.assertEqual([], self.engine.uninstall("123")[1])
        self.assertEqual(before, self.tree_snapshot())

    def test_game_page_returns_free_proxy_names(self):
        (self.game / "WinMM.dll").write_bytes(b"owner OptiScaler")
        with patch.object(main, "require_user"), patch.object(main, "backend", return_value=self.engine), patch.object(metadata, "dlcs", return_value=[]):
            page = asyncio.run(main.Plugin().get_game_page("123"))
        self.assertTrue(page["found"])
        self.assertEqual(["version", "winhttp"], page["available_proxies"])

    def test_darkwood_owner_file_preservation_matrix(self):
        for name in install_state.APIS:
            (self.game / name).unlink()
        files = ("dxgi.dll", "ReShade.ini", "ReShadePreset.ini", "renodx-unityengine.addon64", "Darkwood.exe",
                 "Darkwood_Data/Plugins/x86/steam_api.dll", "Darkwood_Data/Plugins/x86_64/steam_api64.dll")
        for name in files:
            file = self.game / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(("owner " + name).encode())
        before = self.tree_snapshot()
        for mode, proxy in [("direct", "version"), *(("proxy", x) for x in install_state.PROXIES), ("proxy", "dxgi")]:
            with self.subTest(mode=mode, proxy=proxy), patch.object(self.store, "save") as save:
                message = "Invalid SmokeAPI mode or proxy" if proxy == "dxgi" else "Several Steam API DLL folders"
                with self.assertRaisesRegex(ValueError, message):
                    self.engine.install("123", mode, proxy, [], "")
                save.assert_not_called()
                self.assertEqual(before, self.tree_snapshot())
        with self.assertRaisesRegex(ValueError, "No valid install record"):
            self.engine.uninstall("123")
        self.assertEqual(before, self.tree_snapshot())

    def test_remnant_owner_file_preservation_matrix(self):
        folder, = self.unreal_layout("Win64")
        shipping = self.game / "Remnant" / "Binaries" / "Win64"
        shipping.mkdir(parents=True)
        for name in ("Remnant-Win64-Shipping.exe", "dxgi.dll", "winmm.dll"):
            (shipping / name).write_bytes(("owner " + name).encode())
        before = self.tree_snapshot()
        trace = []
        def confined(function, destination=False):
            def call(directory, *args, **kwargs):
                directories = [directory, args[1]] if destination else [directory]
                for current in directories:
                    path = (current.path if hasattr(current, "path")
                            else Path(os.readlink(f"/proc/self/fd/{current.fd}")))
                    if path == self.game or self.game in path.parents:
                        self.assertTrue(path == folder or folder in path.parents, str(path))
                        trace.append((function.__name__, path.relative_to(self.game).as_posix(), args[0]))
                return function(directory, *args, **kwargs)
            return call
        with ExitStack() as stack:
            for method in ("write_new", "raw_rename", "move_to", "raw_delete", "create_probe", "remove_probe", "remove_probe_file"):
                stack.enter_context(patch.object(TestDirectory, method, confined(getattr(TestDirectory, method), method == "move_to")))
            record = self.install()
            self.assertEqual(str(folder), record["game_dir"])
            self.assertTrue(trace)
            installed = self.tree_snapshot()
            for name in installed.keys() - before.keys():
                self.assertEqual(folder, (self.game / name).parent)
            for name in before:
                if name != (folder / "steam_api64.dll").relative_to(self.game).as_posix():
                    self.assertEqual(before[name], installed[name])
            self.assertEqual([], self.engine.uninstall("123")[1])
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual([], smokeapi.available_proxies(str(self.game)))
        for proxy in (*install_state.PROXIES, "dxgi"):
            with self.subTest(proxy=proxy), patch.object(self.store, "save") as save:
                message = "Invalid SmokeAPI mode or proxy" if proxy == "dxgi" else "Proxy mode"
                with self.assertRaisesRegex(ValueError, message):
                    self.engine.install("123", "proxy", proxy, [], "")
                save.assert_not_called()
                self.assertEqual(before, self.tree_snapshot())

    def test_missing_api_refusal_names_the_cause(self):
        for name in install_state.APIS:
            (self.game / name).unlink()
        nested = self.game / "Engine" / "Win64"
        nested.mkdir(parents=True)
        for message, name in (("No supported Windows Steam API DLL", None),
                              ("native Linux build", "libsteam_api.so"),
                              ("unexpected letter case: Steam_api64.dll", "Steam_api64.dll")):
            with self.subTest(message=message):
                if name:
                    (nested / name).write_bytes(b"placeholder")
                with self.assertRaisesRegex(ValueError, message):
                    smokeapi.api_directory(str(self.game))
                if name:
                    (nested / name).unlink()
        deep = self.game.joinpath(*(f"d{n:02d}" for n in range(1, 18)))
        deep.mkdir(parents=True)
        (deep / "steam_api64.dll").write_bytes(b"placeholder")
        with self.assertRaisesRegex(ValueError, "within 16 folders"):
            smokeapi.api_directory(str(self.game))

    def test_library_paths_must_be_absolute(self):
        for path in ("relative",):
            (self.library / "libraryfolders.vdf").write_text('"libraryfolders" { "1" { "path" "' + path.replace("\\", "\\\\") + '" } }')
            with self.assertRaises(ValueError):
                steam_library.find_game("123")

    def test_unavailable_library_does_not_block_internal_game(self):
        path = str(self.base / "missing").replace("\\", "\\\\")
        (self.library / "libraryfolders.vdf").write_text(
            '"libraryfolders" { "1" { "path" "' + path + '" "apps" { "456" "1" } } }')
        self.assertEqual("123", steam_library.find_game("123")["app_id"])
        self.install()
        self.assertEqual("installed", self.engine.status("123")[0])
        self.assertEqual(1, len(steam_library.library_availability()[1]))
        with self.assertRaisesRegex(ValueError, "storage unavailable"):
            steam_library.find_game("456")
        self.assertEqual([], self.engine.uninstall("123")[1])

    def test_save_dlc_selection_is_transactional_and_isolated(self):
        other = self.other_game()
        self.engine.install("456", "direct", "version", ["789"], "")
        other_before = self.tree_snapshot(other)
        self.install()
        dlls = {name: data for name, data in self.snapshot().items() if name.endswith(".dll")}
        saved = self.engine.save_dlc_selection("123", ["987", "654"])
        self.assertEqual(["654", "987"], saved["enabled_dlc_ids"])
        self.assertEqual(saved, self.store.load("123"))
        self.assertEqual("installed", self.engine.status("123")[0])
        config = json.loads((self.game / install_state.CONFIG).read_bytes())
        self.assertEqual({"654": "unlocked", "987": "unlocked"}, config["override_dlc_status"])
        self.assertEqual(dlls, {name: data for name, data in self.snapshot().items() if name.endswith(".dll")})
        self.assertEqual(other_before, self.tree_snapshot(other))
        self.assertNotIn("config_update", saved)

    def test_save_dlc_selection_rolls_back_failed_config_publish(self):
        self.install()
        before = self.snapshot()
        original = TestDirectory.rename
        def fail(directory, source, target, sha):
            if target == install_state.CONFIG and sha != safe_fs.digest(before[install_state.CONFIG]):
                raise OSError("simulated publish failure")
            return original(directory, source, target, sha)
        with patch.object(TestDirectory, "rename", fail):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.engine.save_dlc_selection("123", ["456"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual([], self.store.load("123")["enabled_dlc_ids"])
        self.assertEqual("installed", self.engine.status("123")[0])

    def test_proxy_setup_pending_round_trip_and_acknowledgment(self):
        (self.game / "steam_api.dll").unlink()
        saved = self.install("proxy")
        self.assertEqual("pending", saved["launch_setup"])
        self.assertEqual("pending", self.store.load("123")["launch_setup"])
        self.assertEqual("installed", self.engine.status("123")[0])
        before = self.tree_snapshot()
        self.engine.resolve_setup("123")
        self.assertEqual("acknowledged", self.store.load("123")["launch_setup"])
        self.assertEqual("installed", self.engine.status("123")[0])
        self.assertEqual(before, self.tree_snapshot())

    def test_legacy_proxy_setup_is_pending_until_explicit_acknowledgment(self):
        (self.game / "steam_api.dll").unlink()
        record = self.install("proxy")
        del record["launch_setup"]
        (self.settings / "123.json").write_text(json.dumps(record))
        self.assertEqual("pending", self.store.load("123")["launch_setup"])
        self.engine.resolve_setup("123")
        self.assertEqual("acknowledged", self.store.load("123")["launch_setup"])

    def test_save_dlc_selection_rejects_changed_files_busy_game_and_anti_cheat(self):
        self.install()
        for failure in ("changed", "busy", "anti-cheat"):
            with self.subTest(failure=failure):
                original = (self.game / install_state.CONFIG).read_bytes()
                if failure == "changed":
                    (self.game / install_state.CONFIG).write_bytes(b"owner config")
                if failure == "anti-cheat":
                    (self.game / "EasyAntiCheat").mkdir()
                before = self.tree_snapshot()
                with patch.object(smokeapi, "assert_game_idle", side_effect=ValueError("busy") if failure == "busy" else None):
                    with self.assertRaises(ValueError):
                        self.engine.save_dlc_selection("123", ["456"])
                self.assertEqual(before, self.tree_snapshot())
                self.assertEqual([], self.store.load("123")["enabled_dlc_ids"])
                (self.game / install_state.CONFIG).write_bytes(original)
                if failure == "anti-cheat":
                    (self.game / "EasyAntiCheat").rmdir()

    def test_config_update_crash_recovery_at_every_mutation_boundary(self):
        class Crash(BaseException):
            pass
        self.install()
        original = self.snapshot()
        real_save = self.store.save
        # A power loss bypasses the in-process rollback so the saved journal must suffice.
        for boundary in range(1, 18):
            count = 0
            def crash_after_save(record):
                nonlocal count
                real_save(record)
                if record.get("config_update"):
                    count += 1
                    if count == boundary:
                        raise Crash()
            with self.subTest(boundary=boundary), patch.object(self.store, "save", side_effect=crash_after_save), \
                    patch.object(self.store, "load", wraps=self.store.load) as load:
                # Simulate process death: rollback cannot reopen settings.
                real_load = load._mock_wraps
                def read(value):
                    if count == boundary:
                        raise Crash()
                    return real_load(value)
                load.side_effect = read
                try:
                    self.engine.save_dlc_selection("123", ["456"])
                except Crash:
                    pass
            saved = self.store.load("123")
            if saved.get("config_update"):
                self.assertIn("interrupted", self.engine.status("123")[0])
            # Removal invokes journal recovery before touching any DLLs.
            self.assertEqual([], self.engine.uninstall("123")[1])
            self.assertEqual(self.before, self.snapshot())
            self.install()
            self.assertEqual(original, self.snapshot())

    def interrupt_config_write(self, role="stage", fragment=lambda data: data[:5], verify_intent=True):
        class Crash(BaseException):
            pass
        write, load = TestDirectory.write_new, self.store.load
        interrupted = False
        payload = None
        def fail(directory, name, data):
            nonlocal interrupted, payload
            update = (load("123") or {}).get("config_update", {})
            if name == update.get(role):
                payload = data
                write(directory, name, fragment(data))
                interrupted = True
                raise OSError("interrupted config payload write")
            return write(directory, name, data)
        def reopen(value):
            if interrupted:
                raise Crash()
            return load(value)
        with patch.object(TestDirectory, "write_new", fail), patch.object(self.store, "load", side_effect=reopen):
            with self.assertRaises(Crash):
                self.engine.save_dlc_selection("123", ["456"])
        self.assertTrue(interrupted)
        update = load("123")["config_update"]
        if verify_intent:
            intent = update.get("writes", {}).get(update[role], {})
            self.assertEqual(True, intent.get("began"), "payload ownership must precede the write")
            self.assertEqual(len(payload), intent.get("size"))
            self.assertEqual(safe_fs.digest(payload), intent.get("sha256"))
        return update

    def test_interrupted_config_stage_write_then_save(self):
        self.install()
        dlls = {name: data for name, data in self.snapshot().items() if name.endswith(".dll")}
        update = self.interrupt_config_write(verify_intent=False)
        self.assertEqual(5, (self.game / update["stage"]).stat().st_size)
        saved = self.engine.save_dlc_selection("123", ["456"])
        self.assertEqual(["456"], saved["enabled_dlc_ids"])
        self.assertNotIn("config_update", self.store.load("123"))
        self.assertEqual("installed", self.engine.status("123")[0])
        self.assertEqual(dlls, {name: data for name, data in self.snapshot().items() if name.endswith(".dll")})
        self.assertFalse((self.game / update["stage"]).exists())

    def test_interrupted_config_stage_write_then_remove(self):
        self.install()
        installed = self.snapshot()
        self.interrupt_config_write(verify_intent=False)
        original_remove = self.engine._remove
        def remove(record, directory, **kwargs):
            self.assertEqual(installed[install_state.CONFIG], directory.read(install_state.CONFIG))
            return original_remove(record, directory, **kwargs)
        with patch.object(self.engine, "_remove", side_effect=remove):
            record, warnings = self.engine.uninstall("123")
        self.assertEqual([], warnings)
        self.assertEqual("removed", record["phase"])
        self.assertEqual(self.before, self.snapshot())

    def test_empty_config_stage_write_recovers(self):
        self.install()
        self.interrupt_config_write(fragment=lambda data: b"", verify_intent=False)
        self.assertEqual(["456"], self.engine.save_dlc_selection("123", ["456"])["enabled_dlc_ids"])
        self.assertEqual([], self.engine.uninstall("123")[1])
        self.assertEqual(self.before, self.snapshot())

    def assert_foreign_config_stage_blocks(self, fragment):
        self.install()
        update = self.interrupt_config_write(fragment=fragment)
        before = self.tree_snapshot()
        for action in (lambda: self.engine.save_dlc_selection("123", ["456"]),
                       lambda: self.engine.uninstall("123")):
            with self.assertRaisesRegex(ValueError, "[Pp]reserved"):
                action()
            self.assertEqual(before, self.tree_snapshot())
            self.assertEqual(update, self.store.load("123")["config_update"])

    def test_foreign_config_stage_bytes_are_preserved_and_block(self):
        self.assert_foreign_config_stage_blocks(lambda data: b"alien")

    def test_oversized_config_stage_is_preserved_and_blocks(self):
        self.assert_foreign_config_stage_blocks(lambda data: data + b"extra")

    def test_interrupted_config_backup_write_recovers(self):
        for action in ("save", "remove"):
            with self.subTest(action=action):
                self.install()
                self.interrupt_config_write(role="backup")
                if action == "save":
                    self.assertEqual(["456"], self.engine.save_dlc_selection("123", ["456"])["enabled_dlc_ids"])
                self.assertEqual([], self.engine.uninstall("123")[1])
                self.assertEqual(self.before, self.snapshot())

    def test_nonregular_config_stage_is_preserved_and_blocks(self):
        self.install()
        update = self.interrupt_config_write()
        stage = self.game / update["stage"]
        stage.unlink()
        stage.mkdir()
        before = self.tree_snapshot()
        for action in (lambda: self.engine.save_dlc_selection("123", ["456"]),
                       lambda: self.engine.uninstall("123")):
            with self.assertRaisesRegex(ValueError, "[Pp]reserved"):
                action()
            self.assertEqual(before, self.tree_snapshot())

    def test_unrecorded_config_stage_is_preserved_and_blocks(self):
        self.install()
        update = self.interrupt_config_write()
        unknown = self.game / (".cdh-" + "f" * 32)
        unknown.write_bytes((self.game / update["stage"]).read_bytes())
        before = self.tree_snapshot()
        for action in (lambda: self.engine.save_dlc_selection("123", ["456"]),
                       lambda: self.engine.uninstall("123")):
            with self.assertRaisesRegex(ValueError, "Unrecorded.*preserved"):
                action()
            self.assertEqual(before, self.tree_snapshot())

    def test_config_write_journal_rejects_forged_ownership(self):
        self.install()
        update = self.interrupt_config_write()
        record = self.store.load("123")
        intent = update["writes"][update["stage"]]
        for field, bad in (("began", False), ("size", intent["size"] + 1), ("size", True),
                           ("sha256", update["old"]), ("payload", "alien"),
                           ("partial_size", intent["size"]), ("partial_size", -1)):
            with self.subTest(field=field, bad=bad):
                altered = copy.deepcopy(record)
                altered["config_update"]["writes"][update["stage"]][field] = bad
                with self.assertRaises(ValueError):
                    install_state.validate(altered, "123")
        altered = copy.deepcopy(record)
        altered["config_update"]["writes"][".cdh-" + "e" * 32] = intent
        with self.assertRaises(ValueError):
            install_state.validate(altered, "123")

    def test_partial_config_write_error_rolls_back_without_restart(self):
        for role in ("stage", "backup"):
            with self.subTest(role=role):
                self.install()
                before = self.snapshot()
                write = TestDirectory.write_new
                def fail(directory, name, data):
                    update = (self.store.load("123") or {}).get("config_update", {})
                    if name == update.get(role):
                        write(directory, name, data[:5])
                        raise OSError("interrupted config payload write")
                    return write(directory, name, data)
                with patch.object(TestDirectory, "write_new", fail):
                    with self.assertRaisesRegex(OSError, "interrupted config payload write"):
                        self.engine.save_dlc_selection("123", ["456"])
                self.assertEqual(before, self.snapshot())
                self.assertNotIn("config_update", self.store.load("123"))
                self.assertEqual(["456"], self.engine.save_dlc_selection("123", ["456"])["enabled_dlc_ids"])
                self.assertEqual([], self.engine.uninstall("123")[1])
                self.assertEqual(self.before, self.snapshot())

    def test_interrupted_partial_stage_cleanup_recovers_at_each_journal_boundary(self):
        class Crash(BaseException):
            pass
        for boundary in range(1, 6):
            with self.subTest(boundary=boundary):
                self.install()
                self.interrupt_config_write()
                save = self.store.save
                count = 0
                def crash(record):
                    nonlocal count
                    save(record)
                    count += 1
                    if count == boundary:
                        raise Crash()
                with patch.object(self.store, "save", side_effect=crash):
                    with self.assertRaises(Crash):
                        self.engine.uninstall("123")
                self.assertEqual([], self.engine.uninstall("123")[1])
                self.assertEqual(self.before, self.snapshot())

    def test_config_update_schema_refuses_path_aliases_and_unknown_transitions(self):
        record = self.install()
        update = {"old": record["installed"][install_state.CONFIG], "new": "a" * 64,
                  "ids": ["456"], "stage": ".cdh-" + "1" * 32, "backup": ".cdh-" + "2" * 32,
                  "committed": False}
        for field, bad in (("stage", "../outside"), ("backup", record["stages"][install_state.CONFIG]),
                           ("new", "invalid"), ("ids", ["../123"]), ("committed", 1)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                install_state.validate({**record, "config_update": {**update, field: bad}}, "123")

    def test_dlc_update_new_writer_defers_rollback_until_game_is_closed(self):
        self.install()
        before = self.snapshot()
        checks = 0
        def idle(game, **kwargs):
            nonlocal checks
            checks += 1
            if checks > 1:
                raise ValueError("Close the game first")
        with patch.object(smokeapi, "assert_game_idle", side_effect=idle):
            with self.assertRaisesRegex(ValueError, "Close the game"):
                self.engine.save_dlc_selection("123", ["456"])
        saved = self.store.load("123")
        self.assertIn("config_update", saved)
        self.assertEqual(before[install_state.CONFIG], (self.game / install_state.CONFIG).read_bytes())
        self.engine.save_dlc_selection("123", ["456"])
        self.assertEqual(["456"], self.store.load("123")["enabled_dlc_ids"])
        self.assertEqual("installed", self.engine.status("123")[0])

    def test_proxy_setup_rpc_and_dlc_save_preserve_other_game_with_offline_metadata(self):
        self.other_game()
        self.engine.install("456", "proxy", "version", ["789"], "")
        self.install()
        with patch.object(main, "require_user"), patch.object(main, "backend", return_value=self.engine), \
                patch.object(metadata, "dlcs", return_value={"state": "unavailable", "dlcs": []}):
            plugin = main.Plugin()
            result = asyncio.run(plugin.save_dlc_selection("123", ["654"]))
            self.assertTrue(result["success"])
            first = asyncio.run(plugin.get_game_page("123"))
            second = asyncio.run(plugin.get_game_page("456"))
            self.assertEqual(["654"], first["record"]["enabled_dlc_ids"])
            self.assertEqual("pending", second["record"]["launch_setup"])
            self.assertEqual("unavailable", asyncio.run(plugin.get_game_metadata("123"))["state"])
            acknowledged = asyncio.run(plugin.acknowledge_launch_setup("456"))
            self.assertTrue(acknowledged["success"])
            self.assertEqual(["789"], acknowledged["record"]["enabled_dlc_ids"])
        self.assertEqual("installed", self.engine.status("123")[0])

    def test_restart_recovery_of_interrupted_install(self):
        record = self.install()
        record["phase"] = "installing"
        self.store.save(record)
        self.assertIn("interrupted", self.engine.status("123")[0])
        restarted = smokeapi.Installer(install_state.Store(str(self.settings)), steam_library.find_game)
        self.assertEqual([], restarted.uninstall("123")[1])
        self.assertEqual(self.before, self.snapshot())

    def test_recovery_after_crash_with_only_backups_in_place(self):
        record = self.install()
        self.engine.uninstall("123")
        record["phase"] = "installing"
        self.store.save(record)
        for name in record["originals"]:
            (self.game / name).rename(self.game / name.replace(".dll", "_o.dll"))
        self.assertEqual([], self.engine.uninstall("123")[1])
        self.assertEqual(self.before, self.snapshot())

    def test_recovery_after_crash_during_uninstall(self):
        record = self.install()
        name = "steam_api64.dll"
        (self.game / name).rename(self.game / record["stages"][name])
        record["phase"] = "removing"
        self.store.save(record)
        self.assertEqual([], self.engine.uninstall("123")[1])
        self.assertEqual(self.before, self.snapshot())

    def test_removed_record_never_deletes_new_files(self):
        record = self.install()
        self.engine.uninstall("123")
        (self.game / install_state.CONFIG).write_bytes(b"new unrelated config")
        before = self.snapshot()
        self.engine.uninstall("123")
        self.assertEqual(before, self.snapshot())

    def test_failed_atomic_save_preserves_previous_record(self):
        record = self.install()
        before = (self.settings / "123.json").read_bytes()
        record["phase"] = "removing"
        with patch.object(TestDirectory, "write_new", side_effect=OSError("settings full")):
            with self.assertRaises(OSError):
                self.store.save(record)
        self.assertEqual(before, (self.settings / "123.json").read_bytes())
        self.assertEqual("installed", self.store.load("123")["phase"])

    def test_no_record_config_does_not_mean_installed(self):
        (self.game / install_state.CONFIG).write_bytes(b"foreign")
        self.assertEqual("not installed", self.engine.status("123")[0])
        with self.assertRaises(ValueError):
            self.engine.uninstall("123")
        self.assertEqual(b"foreign", (self.game / install_state.CONFIG).read_bytes())


class ProcessUseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="cdh-proc-", dir=BASE)
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.game = self.base / "Game Files"
        self.game.mkdir()
        self.proc = self.base / "proc"
        self.proc.mkdir()
        self.pid = self.proc / "1234"
        self.pid.mkdir()
        (self.pid / "fd").mkdir()
        (self.pid / "maps").write_text("")
        if os.name == "nt":
            readlink = os.readlink
            patcher = patch.object(steam_library.os, "readlink",
                                   side_effect=lambda path: readlink(path).removeprefix("\\\\?\\"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def check(self, **kwargs):
        steam_library.assert_game_idle({"game_directory": str(self.game)}, str(self.proc), **kwargs)

    def test_cwd_mapped_file_and_open_fd_block_including_deleted_files(self):
        dll = self.game / "game.dll"
        dll.write_bytes(b"held DLL")
        for kind in ("cwd", "maps", "fd", "deleted_fd", "deleted_map"):
            with self.subTest(kind=kind):
                if kind == "cwd":
                    (self.pid / "cwd").symlink_to(self.game, target_is_directory=True)
                elif kind in ("maps", "deleted_map"):
                    path = str(dll).replace(" ", r"\040")
                    if kind == "deleted_map":
                        path += " (deleted)"
                    (self.pid / "maps").write_text(f"100-200 rw-p 0000 00:00 1 {path}\n")
                else:
                    target = str(dll) + (" (deleted)" if kind == "deleted_fd" else "")
                    (self.pid / "fd" / "7").symlink_to(target)
                with self.assertRaisesRegex(ValueError, "Close the game \\(and wait for Steam to finish updating it\\) first"):
                    self.check()
                self.assertEqual(b"held DLL", dll.read_bytes())
                (self.pid / "maps").write_text("")
                for path in (self.pid / "cwd", self.pid / "fd" / "7"):
                    if path.is_symlink():
                        path.unlink()

    def test_prefix_sibling_and_unreadable_pids_do_not_block(self):
        sibling = self.base / "Game Files Other"
        sibling.mkdir()
        (self.pid / "cwd").symlink_to(sibling, target_is_directory=True)
        (self.pid / "maps").write_text(f"100-200 rw-p 0000 00:00 1 {sibling / 'other.dll'}\n")
        (self.proc / "5678").mkdir()
        real_listdir = os.listdir
        def denied(path):
            if str(path) == str(self.proc / "5678" / "fd"):
                raise PermissionError("other user")
            return real_listdir(path)
        with patch.object(steam_library.os, "listdir", side_effect=denied):
            self.check()

    def test_unreadable_proc_refuses_and_readable_fds_still_checked_when_maps_unreadable(self):
        with patch.object(steam_library.os, "listdir", side_effect=PermissionError("proc denied")):
            with self.assertRaisesRegex(ValueError, "cannot inspect /proc"):
                self.check()
        (self.pid / "maps").unlink()
        (self.pid / "fd" / "9").symlink_to(self.game, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Close the game"):
            self.check()

    def test_only_explicit_own_directory_descriptor_is_exempt(self):
        (self.pid / "fd" / "7").symlink_to(self.game, target_is_directory=True)
        self.pid.rename(self.proc / str(os.getpid()))
        self.check(ignored_fds=(7,))
        with self.assertRaisesRegex(ValueError, "Close the game"):
            self.check()

    def test_game_realpath_matches_alias_and_other_pid_cannot_be_exempted(self):
        alias = self.base / "alias"
        alias.symlink_to(self.game, target_is_directory=True)
        (self.pid / "fd" / "7").symlink_to(alias, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Close the game"):
            self.check(ignored_fds=(7,))


class MetadataTests(unittest.TestCase):
    def test_malformed_vdf_refuses_without_hanging(self):
        for text in ('}', '"AppState" {', '"unterminated', '"a" "b" "A" "c"', '"a" { "b" }'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                vdf_lite.parse(text)

    def test_network_failures_are_unavailable_not_zero_dlc(self):
        with patch.object(metadata, "fetch", side_effect=OSError("offline")):
            self.assertEqual({"state": "unavailable", "dlcs": []}, asyncio.run(metadata.dlcs("123")))

    def test_metadata_deadline_returns_without_waiting_for_worker(self):
        started, release = threading.Event(), threading.Event()
        def slow(value):
            started.set()
            release.wait(2)
            return []
        async def check():
            with patch.object(metadata, "lookup", side_effect=slow):
                try:
                    result = await asyncio.wait_for(metadata.dlcs("123", deadline=0.02), 0.5)
                    self.assertTrue(started.is_set())
                    self.assertEqual("unavailable", result["state"])
                finally:
                    release.set()
        asyncio.run(check())

    def test_successful_empty_metadata_is_available(self):
        with patch.object(metadata, "fetch", return_value={"123": {"success": True, "data": {}}}):
            self.assertEqual({"state": "ready", "dlcs": []}, asyncio.run(metadata.dlcs("123")))

    def test_local_status_never_waits_for_metadata(self):
        instance = types.SimpleNamespace(status=lambda value: ("installed", {"enabled_dlc_ids": ["456"]}),
                                         store=types.SimpleNamespace(allowlist=lambda: ["123"]))
        with patch.object(main, "backend", return_value=instance), \
                patch.object(steam_library, "find_game", return_value={"name": "Fake", "game_directory": "fake"}), \
                patch.object(main, "available_proxies", return_value=[]), \
                patch.object(metadata, "dlcs", side_effect=AssertionError("network on local status")):
            result = asyncio.run(main.Plugin().get_game_page("123"))
        self.assertTrue(result["found"])
        self.assertEqual([{"id": "456", "name": "DLC 456"}], result["dlcs"])

    def test_web_only_store_fallback(self):
        with patch.object(metadata, "fetch", side_effect=[{"data": {"123": {"common": {"name": "Example"}}}}, {"123": {"data": {"dlc": [456]}}}]):
            self.assertEqual([{"id": "456", "name": "DLC 456"}], metadata.lookup("123"))

    def test_tls_trusts_the_system_ca_bundle(self):
        # Decky's frozen Python looks for CAs under /usr/lib/ssl, which SteamOS lacks.
        from unittest.mock import Mock
        context = Mock()
        with patch.object(metadata.ssl, "create_default_context", return_value=context),                 patch.object(metadata.os.path, "isfile", side_effect=lambda path: path == metadata.CA_FILES[0]):
            self.assertIs(context, metadata.tls_context())
        context.load_verify_locations.assert_called_once_with(cafile=metadata.CA_FILES[0])

    def test_dlc_names_come_from_steamcmd_and_fall_back_to_ids(self):
        def fetch(url):
            if url.endswith("/123"):
                return {"data": {"123": {"extended": {"listofdlc": "456,789"}}}}
            if url.endswith("/456"):
                return {"data": {"456": {"common": {"name": " Swamps of Corsus "}}}}
            raise OSError("offline")
        with patch.object(metadata, "fetch", side_effect=fetch):
            self.assertEqual([{"id": "456", "name": "Swamps of Corsus"}, {"id": "789", "name": "DLC 789"}],
                             metadata.lookup("123"))

    def test_response_cap_and_timeout(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, count):
                self.count = count
                return b"x" * count
        response = Response()
        with patch.object(metadata.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError, "size limit"):
                metadata.fetch("https://store.steampowered.com/example")
            self.assertEqual(metadata.MAX_RESPONSE + 1, response.count)
            self.assertEqual(10, build.return_value.open.call_args.kwargs["timeout"])

    def test_redirects_rejected(self):
        with self.assertRaises(ValueError):
            metadata.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example")

    def test_production_fails_closed_on_unsupported_platform(self):
        if os.name == "nt":
            with self.assertRaisesRegex(RuntimeError, "require Linux"):
                safe_fs.Directory(str(BASE))
        else:
            with safe_fs.Directory(str(BASE)):
                pass


class RenameCapabilityTests(unittest.TestCase):
    def test_partial_write_preserves_original_error_without_capture_cleanup(self):
        from unittest.mock import Mock
        original = OSError(errno.ENOSPC, "partial write")
        class Stream:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def write(self, data): raise original
        directory = types.SimpleNamespace(fd=7, sync=Mock(side_effect=OSError(errno.EINVAL, "unsupported fsync")))
        with patch.object(safe_fs.os, "open", return_value=9), \
                patch.object(safe_fs.os, "O_NOFOLLOW", 0, create=True), \
                patch.object(safe_fs.os, "fdopen", return_value=Stream()), \
                patch.object(safe_fs.Directory, "delete", side_effect=OSError(errno.EINVAL, "unsupported cleanup")) as delete:
            with self.assertRaises(OSError) as error:
                safe_fs.Directory.write_new(directory, "source", b"probe")
            self.assertIs(original, error.exception)
            directory.sync.assert_not_called()
            delete.assert_not_called()

    def test_cross_directory_rename_uses_destination_descriptor(self):
        from unittest.mock import Mock
        wrapper = Mock(return_value=0)
        with patch.object(safe_fs.ctypes, "CDLL", return_value=types.SimpleNamespace(renameat2=wrapper)):
            safe_fs.rename_noreplace(7, "source", "capture", 11)
        self.assertEqual([7, b"source", 11, b"capture", 1],
                         [x.value for x in wrapper.call_args.args])

    def test_missing_wrapper_uses_architecture_syscall(self):
        from unittest.mock import Mock
        for architecture, number in (("x86_64", 316), ("aarch64", 276)):
            syscall = Mock(return_value=0)
            libc = types.SimpleNamespace(syscall=syscall)
            with self.subTest(architecture=architecture), patch.object(safe_fs.ctypes, "CDLL", return_value=libc), patch.object(safe_fs.platform, "machine", return_value=architecture):
                safe_fs.rename_noreplace(7, "source", "target")
            args = syscall.call_args.args
            self.assertEqual(number, args[0].value)
            self.assertEqual([7, b"source", 7, b"target", 1], [x.value for x in args[1:]])

    def test_unknown_architecture_without_wrapper_refused(self):
        from unittest.mock import Mock
        syscall = Mock()
        with patch.object(safe_fs.ctypes, "CDLL", return_value=types.SimpleNamespace(syscall=syscall)), patch.object(safe_fs.platform, "machine", return_value="riscv64"):
            with self.assertRaisesRegex(RuntimeError, "unavailable on this architecture"):
                safe_fs.rename_noreplace(7, "source", "target")
        syscall.assert_not_called()

    def test_wrapper_and_syscall_errors_never_use_overwriting_rename(self):
        from unittest.mock import Mock
        for wrapper in (True, False):
            call = Mock(return_value=-1)
            libc = types.SimpleNamespace(**({"renameat2": call} if wrapper else {"syscall": call}))
            with self.subTest(wrapper=wrapper), patch.object(safe_fs.ctypes, "CDLL", return_value=libc), patch.object(safe_fs.ctypes, "get_errno", return_value=errno.EOPNOTSUPP), patch.object(safe_fs.platform, "machine", return_value="x86_64"), patch.object(safe_fs.os, "rename") as overwrite:
                with self.assertRaises(OSError) as failure:
                    safe_fs.rename_noreplace(7, "source", "target")
                self.assertEqual(errno.EOPNOTSUPP, failure.exception.errno)
                overwrite.assert_not_called()


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_refuses_root_runtime(self):
        with patch.object(main.os, "geteuid", return_value=0, create=True):
            with self.assertRaisesRegex(RuntimeError, "refuses to run as root"):
                await main.Plugin()._main()

    async def test_backend_initialization_and_every_rpc_refuse_root(self):
        plugin = main.Plugin()
        with patch.object(main.os, "geteuid", return_value=0, create=True), patch.object(main, "Store") as store, patch.object(main.steam_library, "find_game") as find, patch.object(main.metadata, "dlcs") as dlcs:
            with self.assertRaisesRegex(RuntimeError, "refuses to run as root"):
                main.backend()
            calls = (
                plugin.get_game_page("123"),
                plugin.allow_game("123", True),
                plugin.install_smokeapi("123", "direct", "version", [], ""),
                plugin.uninstall_smokeapi("123"),
                plugin.acknowledge_launch_restore("123"),
                plugin.resolve_launch_by_hand("123", True),
            )
            for call in calls:
                result = await call
                self.assertIn("refuses to run as root", result["error"])
                self.assertFalse(result.get("success", result.get("found")))
            store.assert_not_called()
            find.assert_not_called()
            dlcs.assert_not_called()

    async def test_manual_rpc_requires_confirmation(self):
        with patch.object(main.os, "geteuid", return_value=1000, create=True), patch.object(main, "backend") as backend:
            result = await main.Plugin().resolve_launch_by_hand("123", False)
            self.assertFalse(result["success"])
            backend.assert_not_called()

    async def test_rpc_failure_returns_structured_error(self):
        def failure():
            raise ValueError("refused")
        result = await main.Plugin()._operation(failure)
        self.assertEqual({"success": False, "error": "refused"}, result)


if __name__ == "__main__":
    unittest.main()
