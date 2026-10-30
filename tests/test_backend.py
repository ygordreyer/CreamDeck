from __future__ import annotations

import concurrent.futures
import copy
import errno
import json
import os
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

    def snapshot(self):
        return {x.name: x.read_bytes() for x in self.game.iterdir() if x.is_file() and not x.is_symlink()}

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

    def test_malformed_state_and_allowlist_refuse_mutation(self):
        for data in ("{", "{}", "[]", '{"schema": 1}'):
            with self.subTest(data=data):
                (self.settings / "123.json").write_text(data)
                with self.assertRaises((ValueError, TypeError)):
                    self.install()
                with self.assertRaises((ValueError, TypeError)):
                    self.store.allow("456", True)
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

    def test_one_active_game_only(self):
        self.install()
        self.manifest("Example", "456")
        self.store.allow("456", True)
        with self.assertRaisesRegex(ValueError, "one game at a time"):
            self.engine.install("456", "direct", "version", [], "")

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

    def test_library_paths_must_be_absolute_existing(self):
        for path in ("relative", str(self.base / "missing")):
            (self.library / "libraryfolders.vdf").write_text('"libraryfolders" { "1" { "path" "' + path.replace("\\", "\\\\") + '" } }')
            with self.assertRaises(ValueError):
                steam_library.find_game("123")

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

    def test_network_failures_mean_zero_dlc(self):
        with patch.object(metadata, "fetch", side_effect=OSError("offline")):
            self.assertEqual([], metadata.lookup("123"))

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
