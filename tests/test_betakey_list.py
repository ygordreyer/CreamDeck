from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "py_modules"))

import appinfo
import decky
import smokeapi
import steam_library
from fs_adapter import TestDirectory
from test_appinfo import REFUSAL, appinfo_bytes, launch, launch_data

APP_ID = "123456"


class BetakeyListTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="cdh-betakey-", dir=BASE)
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        steam = self.home / ".local" / "share" / "Steam"
        self.library = steam / "steamapps"
        self.game = self.library / "common" / "Placeholder"
        self.game.mkdir(parents=True)
        self.cache = steam / "appcache"
        self.cache.mkdir()
        self.manifest()
        for module in (appinfo, smokeapi, steam_library):
            self.enterContext(patch.object(module, "Directory", TestDirectory))
        self.enterContext(patch.object(decky, "DECKY_USER_HOME", str(self.home)))

    def manifest(self, branch=None):
        extra = '"MountedConfig" { "BetaKey" "' + branch + '" }' if branch is not None else ""
        (self.library / f"appmanifest_{APP_ID}.acf").write_text(
            f'"AppState" {{ "appid" "{APP_ID}" "name" "Placeholder" '
            f'"installdir" "Placeholder" "StateFlags" "4" {extra} }}', encoding="utf-8")

    def write_launches(self, *entries):
        (self.cache / "appinfo.vdf").write_bytes(appinfo_bytes([(APP_ID, launch_data(*entries))]))

    def executables(self):
        return steam_library.launch_executables(APP_ID)

    def test_public_branch_token_matches(self):
        for beta in ("default,test", "default, alpha, release_candidate",
                     "public, warmastered2016", "DEFAULT", " Public ", "default\t",
                     "r5dev public r5-310"):
            with self.subTest(beta=beta):
                self.write_launches(launch("Game.exe", betakey=beta))
                self.assertEqual(["Game.exe"], self.executables())

    def test_public_branch_skips_nonmatching_tokens(self):
        for beta in ("default-test,staging-test", "publicbeta", "nightly",
                     "test huwiz-test silent-review2\t"):
            with self.subTest(beta=beta):
                self.write_launches(launch("Other.exe", betakey=beta), launch("Plain.exe"))
                self.assertEqual(["Plain.exe"], self.executables())

    def test_nonpublic_branch_token_matches(self):
        for branch, beta in (("test", "default,test"), ("test", "profile,test,rating"),
                             ("test", "test huwiz-test silent-review2\t"),
                             ("v1.04_profile", " v1.04_profile"), ("TEST", "default,Test")):
            with self.subTest(branch=branch, beta=beta):
                self.manifest(branch)
                self.write_launches(launch("Game.exe", betakey=beta))
                self.assertEqual(["Game.exe"], self.executables())

    def test_nonpublic_branch_skips_public(self):
        self.manifest("test")
        self.write_launches(launch("Other.exe", betakey="public"), launch("Plain.exe"))
        self.assertEqual(["Plain.exe"], self.executables())

    def test_nonpublic_default_is_unclear_even_with_another_match(self):
        self.manifest("test")
        for beta in ("default", "default, alpha", "DEFAULT"):
            for reverse in (False, True):
                with self.subTest(beta=beta, reverse=reverse):
                    entries = [launch("Other.exe", betakey=beta), launch("Game.exe", betakey="test")]
                    self.write_launches(*(entries[::-1] if reverse else entries))
                    self.assertEqual([], self.executables())

    def test_empty_betakey_is_unclear(self):
        for beta in ("", " ", ",", "\t", ", \t,", ";", "|"):
            with self.subTest(beta=beta):
                self.write_launches(launch("Plain.exe"), launch("Other.exe", betakey=beta))
                self.assertEqual([], self.executables())

    def test_unclear_betakey_on_filtered_entry_has_no_effect(self):
        for beta in ("", " ", ",", "\t", "default", "a|public", "public;test"):
            for selectors in ({"oslist": "linux"}, {"osarch": "32"}, {"osarch": (2, 32)}):
                with self.subTest(beta=beta, selectors=selectors):
                    self.write_launches(launch("Other.exe", betakey=beta, **selectors),
                                        launch("Plain.exe"))
                    self.assertEqual(["Plain.exe"], self.executables())

    def test_semicolon_match_is_unclear_but_nonmatch_is_skipped(self):
        beta = "qa-release-day2; qa-release-day3 "
        for branch, expected in (("qa-release-day2", []), ("qa-release-day3", []),
                                 (None, ["Plain.exe"])):
            with self.subTest(branch=branch):
                self.manifest(branch)
                self.write_launches(launch("Plain.exe"), launch("Other.exe", betakey=beta))
                self.assertEqual(expected, self.executables())

    def test_vertical_bar_match_is_unclear_but_nonmatch_is_skipped(self):
        for beta, expected in (("a|public", []), ("a|b", ["Plain.exe"]),
                               ("default|test", [])):
            with self.subTest(beta=beta):
                self.write_launches(launch("Plain.exe"), launch("Other.exe", betakey=beta))
                self.assertEqual(expected, self.executables())

    def test_unverified_separators_with_nonpublic_default_are_unclear(self):
        self.manifest("test")
        for beta in ("default;alpha", "default|alpha", "default;test", "default|test"):
            with self.subTest(beta=beta):
                self.write_launches(launch("Plain.exe"), launch("Other.exe", betakey=beta))
                self.assertEqual([], self.executables())

    def test_active_branch_with_separators_fails_closed(self):
        for branch in ("public,preview", "beta test", "a;b", "a|b", "test\t", "\ntest",
                       "test\r", "test\v", "test\f"):
            with self.subTest(branch=branch):
                self.manifest(branch)
                self.write_launches(launch("Plain.exe"), launch("Other.exe", betakey=branch))
                self.assertEqual([], self.executables())

    def test_all_supported_whitespace_and_repeated_separators_split_tokens(self):
        for separator in (" ", "\t", "\r", "\n", "\v", "\f", ",", ",, \t,"):
            with self.subTest(separator=separator):
                self.write_launches(launch("Game.exe", betakey=f"alpha{separator}public{separator}beta"))
                self.assertEqual(["Game.exe"], self.executables())

    def test_branch_punctuation_is_matched_exactly(self):
        for branch in ("crossfire-update(cu2)", "drg:s_uci_2026"):
            with self.subTest(branch=branch):
                self.manifest(branch.upper())
                self.write_launches(launch("Game.exe", betakey=f"alpha, {branch},beta"),
                                    launch("Other.exe", betakey=branch + "-extra"))
                self.assertEqual(["Game.exe"], self.executables())

    def test_nioh3_launch_shape(self):
        self.write_launches(
            launch("Nioh3.exe", oslist="windows", osarch=(2, 64), betakey="default,test"),
            launch("Daylight.exe", betakey="profile,test,rating, v1.04_profile"))
        for branch, expected in ((None, ["Nioh3.exe"]), ("test", ["Nioh3.exe", "Daylight.exe"]),
                                 ("profile", [])):
            with self.subTest(branch=branch):
                self.manifest(branch)
                self.assertEqual(expected, self.executables())

    def test_roadcraft_demo_launch_shape(self):
        builders = [f"road_builder_steam_{suffix}.exe" for suffix in ("retail", "profile", "debug")]
        self.write_launches(
            launch(r"root\bin\pc\Roadcraft - Retail.exe",
                   betakey="saber_test, demo_playtest, february25_demo"),
            *(launch(path, oslist="windows", osarch=(2, 64), betakey="saber_test_dev, fe_rc")
              for path in builders))
        for branch, expected in ((None, []), ("demo_playtest", ["root/bin/pc/Roadcraft - Retail.exe"]),
                                 ("fe_rc", builders)):
            with self.subTest(branch=branch):
                self.manifest(branch)
                self.assertEqual(expected, self.executables())

    def test_half_life2_launch_shape(self):
        self.write_launches(
            *(launch("hl2.exe", oslist="windows", betakey=beta)
              for beta in ("default", "steam_legacy", "test")),
            launch("hl2.sh", oslist="linux"), launch("hl2_osx", oslist="macos"))
        self.assertEqual(["hl2.exe"], self.executables())
        self.manifest("steam_legacy")
        self.assertEqual([], self.executables())

    def test_en_garde_demo_launch_shape(self):
        self.write_launches(launch("/EnGarde.exe", oslist="windows", osarch=(2, 64)))
        self.assertEqual(["EnGarde.exe"], self.executables())

    def test_single_leading_slash_normalization(self):
        for path, expected in (("/EnGarde.exe", "EnGarde.exe"), (r"\Bin\Game.exe", "Bin/Game.exe"),
                               ("/./x", "x"), ("/x", "x"),
                               (r"\XCom2-WarOfTheChosen\Binaries\Win64\XCom2.exe",
                                "XCom2-WarOfTheChosen/Binaries/Win64/XCom2.exe"),
                               ("/OdinBlessDLC/OdinBlessDLC.exe", "OdinBlessDLC/OdinBlessDLC.exe")):
            with self.subTest(path=path):
                self.assertEqual(expected, steam_library.relative_executable(path))
                self.write_launches(launch(path))
                self.assertEqual([expected], self.executables())

    def test_unsafe_executables_still_fail_closed(self):
        for path in ("//x", r"\\host\x", "/", "/../x", "/C:/x", "/x/", "steam://open/tools",
                     "steam2ea://launch/123", r"DosBox\\dosbox.exe", "/./../x", "/x\0"):
            with self.subTest(path=path):
                self.assertIsNone(steam_library.relative_executable(path))
                self.write_launches(launch(path))
                self.assertEqual([], self.executables())

    def test_api_directory_resolves_lists_and_leading_slash_and_preserves_refusal(self):
        for name in ("Release", "Ship"):
            folder = self.game / name
            folder.mkdir()
            (folder / "steam_api64.dll").write_bytes(b"original API")
        for entry in (launch("Ship/Game.exe", betakey="default,test"), launch("/Ship/Game.exe")):
            with self.subTest(entry=entry):
                self.write_launches(entry)
                self.assertEqual(str(self.game / "Ship"),
                                 smokeapi.api_directory(str(self.game), launches=self.executables))
        self.write_launches(launch("Ship/Game.exe", betakey="default-test"))
        self.assertEqual([], self.executables())
        with self.assertRaises(ValueError) as raised:
            smokeapi.api_directory(str(self.game), launches=self.executables)
        self.assertEqual(REFUSAL, str(raised.exception))
