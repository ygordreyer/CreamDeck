from __future__ import annotations

from pathlib import Path
import os
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "py_modules"))

import appinfo
import decky
import safe_fs
import smokeapi
import steam_library
from fs_adapter import TestDirectory
from test_appinfo import REFUSAL, appinfo_bytes, launch, launch_data

APP_ID = "274520"


def pe_header(machine=0x8664):
    data = bytearray(0x60)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", data, 0x44, machine)
    return bytes(data)


class UnityArchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="cdh-unity-", dir=BASE)
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        steam = self.home / ".local" / "share" / "Steam"
        library = steam / "steamapps"
        self.game = library / "common" / "Darkwood"
        self.game.mkdir(parents=True)
        self.cache = steam / "appcache"
        self.cache.mkdir()
        (library / f"appmanifest_{APP_ID}.acf").write_text(
            f'"AppState" {{ "appid" "{APP_ID}" "name" "Darkwood" '
            '"installdir" "Darkwood" "StateFlags" "4" }', encoding="utf-8")
        for module in (appinfo, smokeapi, steam_library):
            self.enterContext(patch.object(module, "Directory", TestDirectory))
        self.enterContext(patch.object(decky, "DECKY_USER_HOME", str(self.home)))
        self.x86 = self.api_folder("Darkwood_Data/Plugins/x86", "steam_api.dll")
        self.x64 = self.api_folder("Darkwood_Data/Plugins/x86_64", "steam_api64.dll")
        self.exe = self.game / "Darkwood.exe"
        self.exe.write_bytes(pe_header())

    def api_folder(self, path, name):
        folder = self.game / path
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"original API")
        return folder

    def resolve(self, launches=("Darkwood.exe", "Darkwood.exe")):
        return smokeapi.api_directory(str(self.game), launches=launches)

    def refused(self, launches=("Darkwood.exe",)):
        with self.assertRaises(ValueError) as raised:
            self.resolve(launches)
        self.assertEqual(REFUSAL, str(raised.exception))

    def test_amd64_selects_x64_and_direct_install_preserves_x86(self):
        self.assertEqual(str(self.x64), self.resolve())
        original = (self.x86 / "steam_api.dll").read_bytes()
        store = Mock()
        store.all.return_value = []
        store.allowlist.return_value = [APP_ID]
        engine = smokeapi.Installer(store, steam_library.find_game,
                                    lambda value: ["Darkwood.exe", "Darkwood.exe"])
        with patch.object(smokeapi, "assert_game_idle"), patch.object(
                smokeapi, "verified_resources", return_value={"steam_api64.dll": b"test payload"}):
            record = engine.install(APP_ID, "direct", "version", [], "")
        self.assertEqual(str(self.x64), record["game_dir"])
        self.assertEqual("installed", record["phase"])
        self.assertEqual(record, store.save.call_args.args[0])
        self.assertEqual(original, (self.x86 / "steam_api.dll").read_bytes())
        self.assertEqual(b"test payload", (self.x64 / "steam_api64.dll").read_bytes())

    def test_darkwood_appinfo_integer_arch_entries_resolve_end_to_end(self):
        entries = [
            {"Type": "default", **launch("Darkwood.exe", oslist="windows")},
            {"Type": "none", "Arguments": "-popupwindow",
             **launch("Darkwood.exe", oslist="windows")},
            launch("Darkwood.app", oslist="macos", osarch=(2, 64)),
            launch("Darkwood.x86_64", oslist="linux", osarch=(2, 64)),
        ]
        (self.cache / "appinfo.vdf").write_bytes(appinfo_bytes([(APP_ID, launch_data(*entries))]))
        self.assertEqual(["Darkwood.exe", "Darkwood.exe"], steam_library.launch_executables(APP_ID))
        self.assertEqual(str(self.x64), self.resolve(lambda: steam_library.launch_executables(APP_ID)))

    def test_osarch_strings_integers_and_absent_filter_launches(self):
        for value, applies in ((None, True), ("64", True), ((2, 64), True),
                               ("32", False), ((2, 32), False), ("other", False), ((2, 0), False)):
            with self.subTest(value=value):
                config = {} if value is None else {"osarch": value}
                entries = launch_data(launch("Darkwood.exe", **config))
                (self.cache / "appinfo.vdf").write_bytes(appinfo_bytes([(APP_ID, entries)]))
                self.assertEqual(["Darkwood.exe"] if applies else [],
                                 steam_library.launch_executables(APP_ID))

    def test_osarch_invalid_types_fail_closed_even_when_inactive(self):
        for value in (True, False, 64.0, {}, appinfo.AMBIGUOUS):
            for selectors in ({}, {"oslist": "linux"}, {"betakey": "nightly"}):
                with self.subTest(value=value, selectors=selectors):
                    entries = [launch("Darkwood.exe"),
                               launch("Other.exe", osarch=value, **selectors)]
                    with patch.object(steam_library, "launch_entries", return_value=entries):
                        self.assertEqual([], steam_library.launch_executables(APP_ID))

    def test_non_amd64_machines_refuse(self):
        for machine in (0x014C, 0xAA64, 0x0200):
            with self.subTest(machine=machine):
                self.exe.write_bytes(pe_header(machine))
                self.refused()

    def test_appinfo_64_bit_selector_cannot_replace_pe_evidence(self):
        data = launch_data(launch("Darkwood.exe", oslist="windows", osarch=(2, 64)))
        (self.cache / "appinfo.vdf").write_bytes(appinfo_bytes([(APP_ID, data)]))
        for header in (pe_header(0x014C), b"not a PE file"):
            with self.subTest(header=header):
                self.exe.write_bytes(header)
                self.refused(lambda: steam_library.launch_executables(APP_ID))

    def test_invalid_or_truncated_pe_headers_refuse(self):
        good = pe_header()
        beyond = bytearray(good)
        struct.pack_into("<I", beyond, 0x3C, 4096)
        maximum = bytearray(good)
        struct.pack_into("<I", maximum, 0x3C, 0xFFFFFFFF)
        for data in (b"", good[:0x3F], good[:0x45], b"XX" + good[2:],
                     good[:0x40] + b"XXXX" + good[0x44:], bytes(beyond), bytes(maximum)):
            with self.subTest(data=data):
                self.exe.write_bytes(data)
                self.refused()

    def test_machine_must_fit_inside_head_window(self):
        data = bytearray(5000)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3C, 4091)
        data[4091:4095] = b"PE\0\0"
        struct.pack_into("<H", data, 4095, 0x8664)
        self.exe.write_bytes(data)
        self.refused()

    def test_machine_can_end_at_head_window_boundary(self):
        data = bytearray(5000)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3C, 4090)
        data[4090:4094] = b"PE\0\0"
        struct.pack_into("<H", data, 4094, 0x8664)
        self.exe.write_bytes(data)
        self.assertEqual(str(self.x64), self.resolve())

    def test_large_executable_reads_only_head_and_resolves(self):
        self.exe.write_bytes(pe_header() + b"x" * 8192)
        read_head = TestDirectory.read_head
        calls = []

        def checked(directory, name, cap):
            calls.append((name, cap))
            result = read_head(directory, name, cap)
            self.assertEqual(4096, len(result))
            return result

        with patch.object(TestDirectory, "read_head", checked):
            self.assertEqual(str(self.x64), self.resolve())
        self.assertEqual([("Darkwood.exe", 4096)], calls)

    def test_missing_or_directory_executable_refuses(self):
        self.exe.unlink()
        self.refused()
        self.exe.mkdir()
        self.refused()

    def test_symlink_executable_refuses(self):
        target = self.game / "original.exe"
        self.exe.rename(target)
        try:
            self.exe.symlink_to(target)
        except OSError as error:
            self.skipTest(f"Symlinks unavailable: {error}")
        self.refused()

    def test_read_errors_do_not_leak_exception_text(self):
        for error in (OSError("private read failure"), ValueError("private validation failure")):
            with self.subTest(error=error), patch.object(TestDirectory, "read_head", side_effect=error):
                self.refused()

    def test_distinct_launch_executables_refuse_without_reading(self):
        with patch.object(TestDirectory, "read_head", side_effect=AssertionError("must not read")):
            self.refused(["Darkwood.exe", "Other.exe"])
            self.refused(["Darkwood"])

    def test_relative_executable_normalization_deduplicates_case(self):
        self.assertEqual(str(self.x64), self.resolve(iter([r".\Darkwood.exe", "darkwood.EXE"])))

    def test_third_api_folder_refuses_without_reading(self):
        self.api_folder("Other", "steam_api64.dll")
        with patch.object(TestDirectory, "read_head", side_effect=AssertionError("must not read")):
            self.refused()

    def test_wrong_unity_paths_or_stem_refuse(self):
        data = self.game / "Darkwood_Data"
        for wrong in ("Other_Data", "Darkwood_Other"):
            with self.subTest(wrong=wrong):
                moved = self.game / wrong
                data.rename(moved)
                try:
                    self.refused()
                finally:
                    moved.rename(data)
        plugins = data / "Plugins"
        plugins.rename(data / "Other")
        self.refused()

    def test_x64_folder_requires_exact_64_bit_api_name(self):
        (self.x64 / "steam_api64.dll").unlink()
        (self.x64 / "steam_api.dll").write_bytes(b"32 bit API")
        self.refused()

    def test_nested_launch_folder_and_mixed_case_unity_paths(self):
        nested = self.game / "Build"
        nested.mkdir()
        self.exe.rename(nested / "Darkwood.EXE")
        data = self.game / "Darkwood_Data"
        data.rename(nested / "DARKWOOD_data")
        self.assertEqual(str(nested / "DARKWOOD_data" / "Plugins" / "x86_64"),
                         self.resolve([r"Build\Darkwood.EXE"]))

    def test_default_and_empty_launch_inputs_never_open_executable(self):
        with patch.object(TestDirectory, "read_head", side_effect=AssertionError("must not read")):
            with self.assertRaises(ValueError) as raised:
                smokeapi.api_directory(str(self.game))
            self.assertEqual(REFUSAL, str(raised.exception))
            self.refused([])
            self.assertEqual([], smokeapi.available_proxies(str(self.game)))
            store = Mock()
            store.all.return_value = []
            store.allowlist.return_value = [APP_ID]
            engine = smokeapi.Installer(store, steam_library.find_game)
            with patch.object(smokeapi, "assert_game_idle"), patch.object(
                    smokeapi, "verified_resources", return_value={"steam_api64.dll": b"test payload"}), \
                    self.assertRaises(ValueError) as raised:
                engine.install(APP_ID, "direct", "version", [], "")
            self.assertEqual(REFUSAL, str(raised.exception))

    def test_available_proxies_never_calls_launch_provider(self):
        provider = Mock(side_effect=AssertionError("must not call provider"))
        with patch.object(steam_library, "launch_executables", provider), patch.object(
                TestDirectory, "read_head", side_effect=AssertionError("must not read")):
            self.assertEqual([], smokeapi.available_proxies(str(self.game)))
        provider.assert_not_called()

    def test_multiple_launch_folder_matches_do_not_try_arch_rule(self):
        paths = ["Darkwood_Data/Plugins/x86/Game.exe", "Darkwood_Data/Plugins/x86_64/Game.exe"]
        with patch.object(smokeapi, "unity_amd64_directory", side_effect=AssertionError("must not try arch")):
            self.refused(paths)

    def test_single_api_folder_never_calls_launch_provider(self):
        (self.x86 / "steam_api.dll").unlink()
        provider = Mock(side_effect=AssertionError("must not call provider"))
        with patch.object(TestDirectory, "read_head", side_effect=AssertionError("must not read")):
            self.assertEqual(str(self.x64), self.resolve(provider))
        provider.assert_not_called()

    def test_read_head_is_bounded_and_rejects_unsafe_files(self):
        with TestDirectory(str(self.game)) as directory:
            self.assertEqual(pe_header()[:2], directory.read_head("Darkwood.exe", 2))
            self.assertEqual(b"", directory.read_head("Darkwood.exe", 0))
            for name, cap in (("../Darkwood.exe", 2), ("Darkwood_Data", 2), ("Darkwood.exe", -1)):
                with self.subTest(name=name, cap=cap), self.assertRaises(ValueError):
                    directory.read_head(name, cap)
            with self.assertRaises(FileNotFoundError):
                directory.read_head("missing.exe", 2)

    @unittest.skipUnless(sys.platform == "linux", "Requires Linux dir_fd operations and /proc/self/fd")
    def test_directory_reads_reject_without_leaking_fds(self):
        with safe_fs.Directory(str(self.game)) as directory:
            for method, args in (("read_head", ("Darkwood_Data", 2)),
                                 ("read", ("Darkwood_Data",))):
                with self.subTest(method=method):
                    before = len(os.listdir("/proc/self/fd"))
                    with self.assertRaisesRegex(ValueError, "^Not a regular file: Darkwood_Data$"):
                        getattr(directory, method)(*args)
                    self.assertEqual(before, len(os.listdir("/proc/self/fd")))
