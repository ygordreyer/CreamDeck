from __future__ import annotations

import copy
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "py_modules"))
sys.path.insert(0, str(BASE))

import appinfo
import decky
import main
import smokeapi
import steam_library
from fs_adapter import TestDirectory

APP_ID = "1145350"
REFUSAL = "Several Steam API DLL folders found. Refusing to pick one"


def appinfo_bytes(apps):
    strings = []

    def blob(values):
        result = bytearray()
        for key, value in values.items():
            if key not in strings:
                strings.append(key)
            kind = 0 if isinstance(value, dict) else 1
            if isinstance(value, tuple):
                kind, value = value
            result += struct.pack("<BI", kind, strings.index(key))
            if kind == 0:
                result += blob(value)
            elif kind in (1, 5):
                result += value.encode("utf-16le" if kind == 5 else "utf-8")
                result += b"\0\0" if kind == 5 else b"\0"
            else:
                result += struct.pack({2: "<i", 3: "<f", 4: "<i", 6: "<i", 7: "<Q", 10: "<q"}[kind], value)
        return result + b"\x08"

    entries = bytearray()
    for value, values in apps:
        payload = blob(values)
        metadata = struct.pack("<IIQ20sI20s", 1, 0, 0, b"", 0, b"")
        entries += struct.pack("<II", int(value), len(metadata) + len(payload)) + metadata + payload
    entries += struct.pack("<I", 0)
    table = struct.pack("<I", len(strings)) + b"".join(x.encode() + b"\0" for x in strings)
    return struct.pack("<IIq", appinfo.MAGIC, 1, 16 + len(entries)) + entries + table


def launch_data(*entries):
    return {"AppInfo": {"Config": {"Launch": {str(n): entry for n, entry in enumerate(entries)}}}}


def launch(executable, **config):
    return {"Executable": executable, **({"Config": config} if config else {})}


def hades_launches():
    windows = [("Release", "default", "nightly"), ("Debug", "option1", "nightly"),
               ("Ship", "default", "public"), ("Release", "default", "localization"),
               ("Ship", "default", "test2"), ("Ship", "option2", "nightly"),
               ("Ship", "default", "beta"), ("Ship", "default", "preview")]
    entries = [{"Type": kind, **launch(f"{folder}/Hades2.exe", BetaKey=beta,
                                      oslist="windows", osarch="64")}
               for folder, kind, beta in windows]
    entries += [launch("./Hades II.app", BetaKey=beta, oslist="macos")
                for beta in ("nightly", "public", "preview", "macos")]
    return entries


class AppinfoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cdh-appinfo-", dir=BASE)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.steam = self.home / ".local" / "share" / "Steam"
        self.library = self.steam / "steamapps"
        self.game = self.library / "common" / "Hades II"
        self.game.mkdir(parents=True)
        self.cache = self.steam / "appcache"
        self.cache.mkdir()
        self.file = self.cache / "appinfo.vdf"
        self.manifest()
        for module in (appinfo, smokeapi, steam_library):
            patcher = patch.object(module, "Directory", TestDirectory)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(decky, "DECKY_USER_HOME", str(self.home))
        patcher.start()
        self.addCleanup(patcher.stop)

    def manifest(self, extra="", library=None):
        ((library or self.library) / f"appmanifest_{APP_ID}.acf").write_text(
            f'"AppState" {{ "appid" "{APP_ID}" "name" "Hades II" '
            f'"installdir" "Hades II" "StateFlags" "4" {extra} }}', encoding="utf-8")

    def write_launches(self, *entries):
        self.file.write_bytes(appinfo_bytes([
            ("10", {"unparsed": "ignore me"}),
            (APP_ID, launch_data(*entries)),
            ("20", {"another": {"unrelated": "app"}}),
        ]))

    def folders(self, *names):
        for name in names:
            path = self.game / name
            path.mkdir(parents=True)
            (path / "steam_api64.dll").write_bytes(b"original API")
            (path / "Hades2.exe").write_bytes(b"executable")

    def resolve(self):
        return smokeapi.api_directory(str(self.game), launches=lambda: steam_library.launch_executables(APP_ID))

    def refused(self):
        with self.assertRaises(ValueError) as raised:
            self.resolve()
        self.assertEqual(REFUSAL, str(raised.exception))

    def test_hades_launch_selects_ship_and_install_records_it(self):
        self.folders("Release", "Ship")
        self.write_launches(launch(r".\Ship\Hades2.exe", OSList="Windows"))
        self.assertEqual(["Ship/Hades2.exe"], steam_library.launch_executables(APP_ID))
        self.assertEqual(str(self.game / "Ship"), self.resolve())
        store = Mock()
        store.all.return_value = []
        store.allowlist.return_value = [APP_ID]
        engine = smokeapi.Installer(store, steam_library.find_game, steam_library.launch_executables)
        with patch.object(smokeapi, "assert_game_idle"), patch.object(
                smokeapi, "verified_resources", return_value={"steam_api64.dll": b"test payload"}):
            record = engine.install(APP_ID, "direct", "version", [], "")
        self.assertEqual(str(self.game / "Ship"), record["game_dir"])
        self.assertEqual("installed", record["phase"])
        self.assertEqual(record, store.save.call_args.args[0])
        self.assertEqual(b"original API", (self.game / "Release" / "steam_api64.dll").read_bytes())

    def test_two_applicable_windows_entries_refuse(self):
        self.folders("Release", "Ship")
        self.write_launches(launch("Release/Hades2.exe", oslist="windows"),
                            launch("Ship/Hades2.exe", oslist="windows"))
        self.refused()

    def test_real_hades_launch_shape_uses_installed_branch(self):
        self.folders("Release", "Debug", "Ship")
        entries = hades_launches()
        self.assertEqual(12, len(entries))
        self.write_launches(*entries)
        for branch, paths, folder in (
                (None, ["Ship/Hades2.exe"], "Ship"),
                ("nightly", ["Release/Hades2.exe", "Debug/Hades2.exe", "Ship/Hades2.exe"], None),
                ("beta", ["Ship/Hades2.exe"], "Ship"),
                ("preview", ["Ship/Hades2.exe"], "Ship"),
                ("test2", ["Ship/Hades2.exe"], "Ship"),
                ("localization", ["Release/Hades2.exe"], "Release"),
                ("macos", [], None)):
            with self.subTest(branch=branch):
                self.manifest('"MountedConfig" { "BetaKey" "' + branch + '" }' if branch else "")
                self.assertEqual(paths, steam_library.launch_executables(APP_ID))
                if folder:
                    self.assertEqual(str(self.game / folder), self.resolve())
                else:
                    self.refused()

    def test_darkwood_launch_shape_ignores_non_windows_entries(self):
        self.folders("Release", "Ship")
        self.write_launches(
            {"Type": "default", **launch("Ship/Darkwood.exe", oslist="windows")},
            {"Type": "none", **launch("Ship/Darkwood.exe", oslist="windows")},
            launch("Release/Darkwood.app", oslist="macos"),
            launch("Release/Darkwood.x86_64", oslist="linux"))
        self.assertEqual(["Ship/Darkwood.exe", "Ship/Darkwood.exe"],
                         steam_library.launch_executables(APP_ID))
        self.assertEqual(str(self.game / "Ship"), self.resolve())

    def test_linux_ignored_and_windows_comma_list_accepted(self):
        self.folders("Release", "Ship")
        self.write_launches(launch("Release/Hades2.exe", oslist="linux"),
                            launch("Ship/Hades2.exe", oslist="linux, WINDOWS"))
        self.assertEqual(["Ship/Hades2.exe"], steam_library.launch_executables(APP_ID))
        self.assertEqual(str(self.game / "Ship"), self.resolve())

    def test_beta_entries_require_matching_manifest_branch(self):
        self.folders("Release", "Ship")
        self.write_launches(launch("Ship/Hades2.exe", betakey="Preview"))
        for extra, applicable in (
                ("", False), ('"MountedConfig" { "BetaKey" "other" }', False),
                ('"MountedConfig" { "BetaKey" "pREVIEW" }', True),
                ('"UserConfig" { "BetaKey" "preview" }', True),
                ('"MountedConfig" { "BetaKey" "other" } "UserConfig" { "BetaKey" "preview" }', False)):
            with self.subTest(extra=extra):
                self.manifest(extra)
                self.assertEqual(["Ship/Hades2.exe"] if applicable else [],
                                 steam_library.launch_executables(APP_ID))
                if applicable:
                    self.assertEqual(str(self.game / "Ship"), self.resolve())
                else:
                    self.refused()

    def test_default_public_branch_and_empty_branch_fallback(self):
        self.folders("Release", "Ship")
        for extra, beta, applicable in (
                ("", "public", True),
                ('"MountedConfig" { "BetaKey" "" }', "public", True),
                ('"UserConfig" { "BetaKey" "" }', "PUBLIC", True),
                ('"MountedConfig" { "BetaKey" "" } "UserConfig" { "BetaKey" "preview" }', "Preview", True),
                ('"MountedConfig" { "BetaKey" "nightly" } "UserConfig" { "BetaKey" "preview" }', "nightly", True),
                ("", "public,preview", False),
                ('"MountedConfig" { "BetaKey" "public,preview" }', "public", False)):
            with self.subTest(extra=extra, beta=beta):
                self.manifest(extra)
                self.write_launches(launch("Ship/Hades2.exe", BetaKey=beta))
                self.assertEqual(["Ship/Hades2.exe"] if applicable else [],
                                 steam_library.launch_executables(APP_ID))
                if applicable:
                    self.assertEqual(str(self.game / "Ship"), self.resolve())
                else:
                    self.refused()

    def test_non_string_manifest_betakey_anywhere_fails_closed(self):
        self.folders("Release", "Ship")
        self.write_launches(launch("Ship/Hades2.exe"))
        for extra in ('"MountedConfig" { "BetaKey" {} }', '"UserConfig" { "BetaKey" {} }',
                      '"MountedConfig" { "BetaKey" "public" } "UserConfig" { "BetaKey" {} }',
                      '"MountedConfig" { "BetaKey" {} } "UserConfig" { "BetaKey" "public" }'):
            with self.subTest(extra=extra):
                self.manifest(extra)
                self.assertEqual([], steam_library.launch_executables(APP_ID))
                self.refused()

    def test_osarch_selects_64_bit_and_absent_still_applies(self):
        self.folders("Bin32", "Bin64")
        self.write_launches(launch("Bin32/Game.exe", osarch="32"),
                            launch("Bin64/Game.exe", osarch="64"))
        self.assertEqual(["Bin64/Game.exe"], steam_library.launch_executables(APP_ID))
        self.assertEqual(str(self.game / "Bin64"), self.resolve())
        self.write_launches(launch("Bin64/Game.exe"))
        self.assertEqual(["Bin64/Game.exe"], steam_library.launch_executables(APP_ID))
        self.assertEqual(str(self.game / "Bin64"), self.resolve())

    def test_non_string_launch_selectors_fail_closed_even_on_inactive_entries(self):
        self.folders("Release", "Ship")
        for field in ("BetaKey", "osarch", "oslist"):
            for value in ({}, (2, 64)):
                for selectors in ({}, {"oslist": "linux"}, {"BetaKey": "other"}):
                    with self.subTest(field=field, value=value, selectors=selectors):
                        self.write_launches(launch("Ship/Hades2.exe"),
                                            launch("Release/Hades2.exe", **{**selectors, field: value}))
                        self.assertEqual([], steam_library.launch_executables(APP_ID))
                        self.refused()

    def test_duplicate_unrelated_keys_preserve_hades_launch_selection(self):
        self.folders("Release", "Debug", "Ship")
        registries = [
            {r"hkey_local_machine\software\activision\prototype 2": {"value": "one"},
             r"HKEY_LOCAL_MACHINE\SOFTWARE\ACTIVISION\PROTOTYPE 2": {"value": "two"}},
            {r"hkey_local_machine\software\legacy of kain": {"dword": "1", "DWORD": "2"}},
        ]
        for registry in registries:
            with self.subTest(registry=registry):
                values = launch_data(*hades_launches())
                values["AppInfo"]["Install"] = {"Registry": registry}
                values["name"] = "one"
                values["NAME"] = "two"
                values["AppInfo"]["Config"]["Launch"]["0"].update(
                    {"Description": "one", "DESCRIPTION": "two"})
                self.file.write_bytes(appinfo_bytes([(APP_ID, values)]))
                self.assertEqual(12, len(appinfo.launch_entries(str(self.cache), APP_ID)))
                self.assertEqual(["Ship/Hades2.exe"], steam_library.launch_executables(APP_ID))
                self.assertEqual(str(self.game / "Ship"), self.resolve())

    def test_duplicate_launch_path_keys_fail_closed_even_on_inactive_entries(self):
        self.folders("Release", "Ship")
        original = launch_data(launch("Ship/Hades2.exe"),
                               launch("Release/Hades2.exe", BetaKey="nightly", oslist="linux", osarch="32"))
        # Include both first-scalar and first-section duplicates. Neither order
        # may leave a usable launch path after a case-insensitive lookup.
        cases = [((), "appinfo", {}), (("AppInfo",), "config", {}),
                 (("AppInfo", "Config"), "launch", {}),
                 (("AppInfo", "Config", "Launch"), "A", {}),
                 (("AppInfo", "Config", "Launch", "1"), "executable", "Ship/Hades2.exe"),
                 (("AppInfo", "Config", "Launch", "1"), "config", "scalar")]
        cases += [(("AppInfo", "Config", "Launch", "1", "Config"), field, "public")
                  for field in ("betakey", "OSList", "OSArch")]
        for path, spelling, value in cases:
            for reverse in (False, True):
                with self.subTest(path=path, spelling=spelling, reverse=reverse):
                    values = copy.deepcopy(original)
                    section = values
                    for key in path:
                        section = section[key]
                    if spelling == "A":
                        section["a"] = launch("Ship/Hades2.exe")
                    section[spelling] = value
                    if reverse:
                        reordered = list(section.items())[::-1]
                        section.clear()
                        section.update(reordered)
                    self.file.write_bytes(appinfo_bytes([(APP_ID, values)]))
                    self.assertEqual([], steam_library.launch_executables(APP_ID))
                    self.refused()

    def test_unicode_lookalike_keys_do_not_hide_launch_selectors(self):
        self.folders("Release", "Ship")
        for name, lookalike, ignored, selector in (
                ("config", "con\ufb01g", {"BetaKey": "public"}, {"BetaKey": "nightly"}),
                ("oslist", "o\u017flist", "windows", "linux"),
                ("osarch", "o\u017farch", "64", "32"),
                ("BetaKey", "Beta\uff2bey", "public", "nightly")):
            for reverse in (False, True):
                with self.subTest(name=name, reverse=reverse):
                    self.assertNotEqual(name.lower(), lookalike.lower())
                    selectors = {lookalike: ignored, name: selector}
                    if reverse:
                        selectors = dict(reversed(list(selectors.items())))
                    entry = {"Executable": "Ship/Hades2.exe"}
                    entry.update(selectors if name == "config" else {"Config": selectors})
                    self.write_launches(entry)
                    parsed = appinfo.launch_entries(str(self.cache), APP_ID)[0]
                    section = parsed if name == "config" else parsed["Config"]
                    self.assertEqual(list(selectors), list(section)[1:] if name == "config" else list(section))
                    self.assertEqual(selector, appinfo.get_child(section, name))
                    self.assertEqual(ignored, section[lookalike])
                    self.assertEqual([], steam_library.launch_executables(APP_ID))
                    self.refused()

    def test_lower_equivalent_launch_keys_are_ambiguous_with_two_or_three_spellings(self):
        self.folders("Release", "Ship")
        for spellings, value in ((["Config", "config", "CONFIG"], {"BetaKey": "nightly"}),
                                 (["OSList", "oslist", "OSLIST"], "linux"),
                                 (["OSArch", "osarch", "OSARCH"], "32"),
                                 (["BetaKey", "betakey", "BETAKEY"], "nightly"),
                                 (["Beta\u212aey", "BetaKey", "BETAKEY"], "nightly")):
            for count in (2, 3):
                for reverse in (False, True):
                    with self.subTest(spellings=spellings, count=count, reverse=reverse):
                        keys = spellings[:count][::-1] if reverse else spellings[:count]
                        selectors = dict.fromkeys(keys, value)
                        entry = {"Executable": "Ship/Hades2.exe"}
                        entry.update(selectors if keys[0].lower() == "config" else {"Config": selectors})
                        self.write_launches(entry)
                        with self.assertRaisesRegex(ValueError, "Ambiguous appinfo launch"):
                            appinfo.launch_entries(str(self.cache), APP_ID)
                        self.assertEqual([], steam_library.launch_executables(APP_ID))
                        self.refused()

    def test_unsafe_executables_are_ignored(self):
        self.folders("Release", "Ship")
        for path in ("../Ship/Hades2.exe", "Ship/../Hades2.exe", "/x", r"C:\x", "C:x",
                     "Ship//Hades2.exe", "Ship/./Hades2.exe", "", "Ship/", r"\\host\x"):
            with self.subTest(path=path):
                self.write_launches(launch(path))
                self.assertEqual([], steam_library.launch_executables(APP_ID))
                self.refused()
        self.write_launches(launch("../Release/Hades2.exe"), launch("Ship/Hades2.exe"))
        self.assertEqual(str(self.game / "Ship"), self.resolve())

    def test_matching_is_case_insensitive_and_requires_exact_folder(self):
        self.folders("Release", "Ship")
        self.write_launches(launch("sHIP/Hades2.exe"), launch("SHIP/Other.exe"))
        self.assertEqual(str(self.game / "Ship"), self.resolve())
        self.write_launches(launch("Ship/subfolder/Hades2.exe"))
        self.refused()

    def test_missing_and_malformed_appinfo_fail_closed(self):
        self.folders("Release", "Ship")
        good = appinfo_bytes([(APP_ID, launch_data(launch("Ship/Hades2.exe")))])
        deep = {"leaf": "value"}
        for _ in range(appinfo.MAX_DEPTH + 1):
            deep = {"nested": deep}
        malformed = {
            "wrong magic": b"\0\0\0\0" + good[4:],
            "truncated header": good[:12],
            "truncated entry": good[:22],
            "size past file": good[:20] + struct.pack("<I", len(good) * 2) + good[24:],
            "short metadata": good[:20] + struct.pack("<I", 59) + good[24:],
            "string index": good[:85] + struct.pack("<I", 0xFFFFFFFF) + good[89:],
            "unknown type": good[:84] + b"\x09" + good[85:],
            "deep nesting": appinfo_bytes([(APP_ID, deep)]),
            "missing app": appinfo_bytes([("10", launch_data(launch("Ship/Hades2.exe")))]),
            "negative table offset": good[:8] + struct.pack("<q", -1) + good[16:],
            "table past file": good[:8] + struct.pack("<q", len(good) + 1) + good[16:],
            "unterminated table string": good[:-1],
            "bad UTF8": good[:-2] + b"\xff\0",
            "missing launch config": appinfo_bytes([(APP_ID, {"appinfo": {}})]),
        }
        for label, data in [("missing file", None), *malformed.items()]:
            with self.subTest(label=label):
                if data is not None:
                    self.file.write_bytes(data)
                with self.assertRaises(ValueError):
                    appinfo.launch_entries(str(self.cache), APP_ID)
                self.assertEqual([], steam_library.launch_executables(APP_ID))
                self.refused()

    def test_every_truncation_is_value_error(self):
        data = appinfo_bytes([(APP_ID, launch_data(launch("Ship/Hades2.exe")))])
        for length in range(len(data)):
            with self.subTest(length=length):
                self.file.write_bytes(data[:length])
                with self.assertRaises(ValueError):
                    appinfo.launch_entries(str(self.cache), APP_ID)

    def test_numeric_and_wide_values_and_alternate_section_end(self):
        values = launch_data(launch("Ship/Hades2.exe"))
        values["scalars"] = {str(kind): (kind, value) for kind, value in
                             ((2, -1), (3, 1.25), (4, 4), (5, "wide λ"),
                              (6, 255), (7, 2 ** 63), (10, -2 ** 40))}
        data = appinfo_bytes([(APP_ID, values)])
        offset = struct.unpack_from("<q", data, 8)[0]
        data = data[:offset - 5] + b"\x0b" + data[offset - 4:]
        self.file.write_bytes(data)
        self.assertEqual([launch("Ship/Hades2.exe")], appinfo.launch_entries(str(self.cache), APP_ID))

    def test_parser_rejects_limits_and_invalid_strings(self):
        self.write_launches(launch("Ship/Hades2.exe"))
        good = self.file.read_bytes()
        offset = struct.unpack_from("<q", good, 8)[0]
        cases = [good[:offset] + struct.pack("<I", 0xFFFFFFFF) + good[offset + 4:],
                 appinfo_bytes([(APP_ID, launch_data(launch("x" * (appinfo.MAX_STRING + 1))))]),
                 appinfo_bytes([(APP_ID, {**launch_data(launch("Ship/Hades2.exe")),
                                          "wide": (5, "A" * (appinfo.MAX_STRING // 2 + 1))})]),
                 appinfo_bytes([(APP_ID, {"wide": (5, "A")})]).replace(b"A\0\0\0", b"\0\xd8\0\0")]
        for data in cases:
            with self.subTest(length=len(data)):
                self.file.write_bytes(data)
                with self.assertRaises(ValueError):
                    appinfo.launch_entries(str(self.cache), APP_ID)
        self.file.write_bytes(good)
        with patch.object(appinfo, "MAX_SIZE", len(good) - 1):
            with self.assertRaises(ValueError):
                appinfo.launch_entries(str(self.cache), APP_ID)
        with patch.object(appinfo, "MAX_COUNT", 1):
            with self.assertRaises(ValueError):
                appinfo.launch_entries(str(self.cache), APP_ID)

    def test_value_budget_is_independent_of_string_table_count(self):
        values = launch_data(*(launch("Ship/Hades2.exe") for _ in range(4)))
        data = appinfo_bytes([(APP_ID, values)])
        offset = struct.unpack_from("<q", data, 8)[0]
        string_count = struct.unpack_from("<I", data, offset)[0]
        self.assertEqual(8, string_count)
        self.file.write_bytes(data)
        with patch.object(appinfo, "MAX_COUNT", string_count):
            with self.assertRaisesRegex(ValueError, "Too many appinfo values"):
                appinfo.launch_entries(str(self.cache), APP_ID)
        self.assertEqual(4, len(appinfo.launch_entries(str(self.cache), APP_ID)))

    def test_table_keys_are_lowered_once_and_duplicates_keep_first_spelling(self):
        folds = []
        parser_folds = []

        class CountedKey(str):
            def lower(self):
                folds.append(str(self))
                return super().lower()

        read_string = appinfo.Reader.string

        def counted_string(reader, wide=False):
            return CountedKey(read_string(reader, wide))

        get_child = appinfo.get_child

        def lookup(*args):
            # Lookup also calls lower(); count only normalization before lookup.
            if not parser_folds:
                parser_folds.extend(folds)
            return get_child(*args)

        self.write_launches(*(launch("Ship/Hades2.exe", oslist="windows") for _ in range(8)))
        data = self.file.read_bytes()
        offset = struct.unpack_from("<q", data, 8)[0]
        count = struct.unpack_from("<I", data, offset)[0]
        with patch.object(appinfo.Reader, "string", counted_string), patch.object(appinfo, "get_child", lookup):
            self.assertEqual(8, len(appinfo.launch_entries(str(self.cache), APP_ID)))
        self.assertEqual(count, len(parser_folds))
        self.assertEqual(1, parser_folds.count("Executable"))
        self.assertEqual(1, parser_folds.count("Config"))
        parsed = appinfo.pairs(appinfo.Reader(
            b"\x01\0\0\0\0one\0\x01\x01\0\0\0two\0\x01\x02\0\0\0three\0\x08"),
            [("BetaKey", "betakey"), ("BETAKEY", "betakey"), ("betakey", "betakey")])
        self.assertEqual(["BetaKey"], list(parsed))
        self.assertIs(appinfo.AMBIGUOUS, parsed["BetaKey"])
        self.assertIs(appinfo.AMBIGUOUS, appinfo.get_child(parsed, "betakey"))
        self.assertFalse(isinstance(appinfo.AMBIGUOUS, (str, dict)))

    def test_trailing_table_bytes_and_app_list_junk_raise_and_refuse(self):
        self.folders("Release", "Ship")
        good = appinfo_bytes([(APP_ID, launch_data(launch("Ship/Hades2.exe")))])
        offset = struct.unpack_from("<q", good, 8)[0]
        gap = b"junk"
        adjusted = good[:8] + struct.pack("<q", offset + len(gap)) + good[16:offset] + gap + good[offset:]
        for data in (good + b"junk", adjusted):
            with self.subTest(data=data):
                self.file.write_bytes(data)
                with self.assertRaises(ValueError):
                    appinfo.launch_entries(str(self.cache), APP_ID)
                self.assertEqual([], steam_library.launch_executables(APP_ID))
                self.refused()

    def test_available_proxies_never_calls_launch_provider_or_opens_appinfo(self):
        self.folders("Release", "Ship")
        self.write_launches(*hades_launches())
        provider = Mock(side_effect=AssertionError("must not call launch provider"))
        with patch.object(steam_library, "launch_executables", provider), patch.object(
                appinfo, "Directory", side_effect=AssertionError("must not open appinfo")):
            self.assertEqual([], smokeapi.available_proxies(str(self.game)))
        provider.assert_not_called()

    def test_no_or_single_api_folder_never_reads_appinfo(self):
        self.write_launches(launch("Ship/Hades2.exe"))
        with patch.object(appinfo, "Directory", side_effect=AssertionError("must not open appinfo")):
            with self.assertRaisesRegex(ValueError, "No supported Windows Steam API"):
                self.resolve()
            folder = "Engine/Binaries/ThirdParty/Steamworks/Steamv139/Win64"
            self.folders(folder)
            self.assertEqual(str(self.game / folder), self.resolve())
            store = Mock()
            store.all.return_value = []
            store.allowlist.return_value = [APP_ID]
            engine = smokeapi.Installer(store, steam_library.find_game, steam_library.launch_executables)
            with patch.object(smokeapi, "assert_game_idle"), patch.object(
                    smokeapi, "verified_resources", return_value={"steam_api64.dll": b"test payload"}):
                self.assertEqual(str(self.game / folder), engine.install(APP_ID, "direct", "version", [], "")["game_dir"])
            (self.game / "steam_api64.dll").write_bytes(b"top-level API")
            self.assertEqual(str(self.game), self.resolve())

    def test_case_only_api_folders_refused_on_all_hosts(self):
        class CaseDirectory:
            def __init__(self, path):
                self.root = True

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def exists(self, name):
                return False

            def entries(self):
                if self.root:
                    return [(x, os.stat_result((stat.S_IFDIR, 0, 0, 0, 0, 0, 0, 0, 0, 0)))
                            for x in ("Ship", "ship", "Release")]
                return [("steam_api64.dll", os.stat_result((stat.S_IFREG, 0, 0, 0, 0, 0, 0, 0, 0, 0)))]

            def child(self, name):
                child = CaseDirectory(name)
                child.root = False
                return child

        with patch.object(smokeapi, "Directory", CaseDirectory):
            for paths in (["Ship/Hades2.exe"], ["Release/Hades2.exe"]):
                with self.subTest(paths=paths), self.assertRaisesRegex(ValueError, REFUSAL):
                    smokeapi.api_directory(str(self.game), launches=paths)

    def test_secondary_library_uses_primary_steam_appcache(self):
        self.write_launches(launch("Ship/Hades2.exe"))
        other = self.base / "SteamLibrary"
        library = other / "steamapps"
        library.mkdir(parents=True)
        self.manifest(library=library)
        (self.library / f"appmanifest_{APP_ID}.acf").unlink()
        path = str(other).replace("\\", "\\\\")
        (self.library / "libraryfolders.vdf").write_text(f'"libraryfolders" {{ "1" {{ "path" "{path}" }} }}')
        self.assertEqual(["Ship/Hades2.exe"], steam_library.launch_executables(APP_ID))

    def test_manifest_read_and_parse_errors_return_empty(self):
        self.write_launches(launch("Ship/Hades2.exe"))
        manifest = self.library / f"appmanifest_{APP_ID}.acf"
        for data in (b"\xff", b'"AppState" {', b'"AppState" { "appid" "10" }'):
            with self.subTest(data=data):
                manifest.write_bytes(data)
                self.assertEqual([], steam_library.launch_executables(APP_ID))
        manifest.unlink()
        self.assertEqual([], steam_library.launch_executables(APP_ID))

    def test_backend_wires_launch_provider_without_changing_game_shape(self):
        expected = {"app_id", "name", "game_directory", "updating", "update_state_checked"}
        self.assertEqual(expected, set(steam_library.find_game(APP_ID)))
        with patch.object(main, "require_user"), patch.object(main, "Store") as store:
            engine = main.backend()
        self.assertIs(engine.store, store.return_value)
        self.assertIs(engine.launch_executables, steam_library.launch_executables)
