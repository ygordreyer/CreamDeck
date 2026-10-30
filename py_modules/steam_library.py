"""Read one Steam manifest using Decky's user home and confined library paths."""
from __future__ import annotations

import os
import re
import decky
from appinfo import launch_entries
from safe_fs import Directory, app_id, component
from vdf_lite import get_child, parse

BUSY_MESSAGE = "Close the game (and wait for Steam to finish updating it) first"
# EAppState UpdateRunning, UpdateStarted, Reconfiguring, Validating,
# AddingFiles, Preallocating, Downloading, Staging, Committing, UpdateStopping.
UPDATE_RUNNING_FLAGS = 256 | 1024 | 0xFF0000


def assert_game_idle(game, proc_path="/proc", ignored_fds=()):
    if game.get("updating"):
        raise ValueError(BUSY_MESSAGE)
    game_path = os.path.realpath(game["game_directory"])

    def under_game(path):
        path = path.removesuffix(" (deleted)")
        if not os.path.isabs(path):
            return False
        try:
            return os.path.commonpath([game_path, os.path.realpath(path)]) == game_path
        except ValueError:
            return False

    try:
        pids = os.listdir(proc_path)
    except OSError as error:
        raise ValueError(BUSY_MESSAGE + ": cannot inspect /proc") from error
    for pid in pids:
        if not pid.isascii() or not pid.isdecimal():
            continue
        folder = os.path.join(proc_path, pid)
        try:
            cwd = os.readlink(os.path.join(folder, "cwd"))
        except OSError:
            pass
        else:
            if under_game(cwd):
                raise ValueError(BUSY_MESSAGE)
        try:
            with open(os.path.join(folder, "maps"), encoding="utf-8", errors="surrogateescape") as stream:
                for line in stream:
                    fields = line.split(None, 5)
                    if len(fields) == 6:
                        path = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[5].rstrip("\n"))
                        if under_game(path):
                            raise ValueError(BUSY_MESSAGE)
        except OSError:
            pass
        try:
            fds = os.listdir(os.path.join(folder, "fd"))
        except OSError:
            continue
        for fd in fds:
            if pid == str(os.getpid()) and fd in {str(x) for x in ignored_fds}:
                continue
            try:
                path = os.readlink(os.path.join(folder, "fd", fd))
            except OSError:
                continue
            if under_game(path):
                raise ValueError(BUSY_MESSAGE)


def steam_root():
    home = getattr(decky, "DECKY_USER_HOME", None)
    if not home or not os.path.isabs(home):
        raise ValueError("Decky user-home helper is unavailable")
    # Steam's home aliases may themselves be symlinks. Resolve that trusted
    # discovery alias once, then use descriptor walks for every library access.
    return next((os.path.realpath(os.path.join(home, suffix)) for suffix in
                  (".local/share/Steam", ".steam/steam", ".steam/root")
                  if os.path.isdir(os.path.join(home, suffix))), None)


def library_folders():
    steam = steam_root()
    if not steam:
        return []
    folders = [os.path.join(steam, "steamapps")]
    with Directory(folders[0]) as directory:
        if directory.exists("libraryfolders.vdf"):
            data = get_child(parse(directory.read("libraryfolders.vdf").decode("utf-8")), "libraryfolders")
            if not isinstance(data, dict):
                raise ValueError("Invalid Steam library list")
            for key, entry in data.items():
                if not key.isdecimal() or not isinstance(entry, dict):
                    continue
                path = get_child(entry, "path")
                if not isinstance(path, str) or not os.path.isabs(path) or not os.path.isdir(path):
                    raise ValueError("Steam library must be absolute and existing")
                folder = os.path.join(path, "steamapps")
                with Directory(folder):
                    pass
                if folder not in folders:
                    folders.append(folder)
    return folders


def confined_game(library, install_dir):
    component(install_dir)
    common = os.path.join(library, "common")
    path = os.path.join(common, install_dir)
    if os.path.commonpath([os.path.realpath(common), os.path.realpath(path)]) != os.path.realpath(common):
        raise ValueError("Game directory escapes steamapps/common")
    try:
        with Directory(path):
            pass
    except FileNotFoundError as error:
        raise ValueError("Steam game is not installed: game folder is missing") from error
    return path


def find_game(value):
    value = app_id(value)
    for library in library_folders():
        with Directory(library) as directory:
            name = f"appmanifest_{value}.acf"
            if not directory.exists(name):
                continue
            data = get_child(parse(directory.read(name).decode("utf-8")), "AppState")
        if not isinstance(data, dict) or get_child(data, "appid") != value:
            raise ValueError("Manifest app ID mismatch")
        flags = get_child(data, "StateFlags")
        if flags is not None and (not isinstance(flags, str) or not re.fullmatch(r"[0-9]+", flags)):
            raise ValueError("Invalid manifest StateFlags")
        updating = bool(int(flags) & UPDATE_RUNNING_FLAGS) if flags is not None else False
        if flags is not None and not int(flags) & 4 and not updating:
            raise ValueError("Steam game is not installed")
        return {"app_id": value, "name": get_child(data, "name") or value,
                "game_directory": confined_game(library, get_child(data, "installdir")),
                "updating": updating,
                "update_state_checked": flags is not None}
    raise ValueError("Installed Steam game not found")


def relative_executable(value):
    if not isinstance(value, str):
        return None
    value = value.replace("\\", "/").removeprefix("./")
    if ":" in value or "\0" in value or any(x in ("", ".", "..") for x in value.split("/")):
        return None
    return value


def launch_executables(value):
    try:
        value = app_id(value)
        manifest = None
        for library in library_folders():
            with Directory(library) as directory:
                name = f"appmanifest_{value}.acf"
                if directory.exists(name):
                    manifest = get_child(parse(directory.read(name).decode("utf-8")), "AppState")
                    break
        if not isinstance(manifest, dict) or get_child(manifest, "appid") != value:
            return []
        mounted = get_child(manifest, "MountedConfig", "BetaKey")
        user = get_child(manifest, "UserConfig", "BetaKey")
        if any(branch is not None and not isinstance(branch, str) for branch in (mounted, user)):
            return []
        branch = mounted or user or "public"
        result = []
        for entry in launch_entries(os.path.join(steam_root(), "appcache"), value):
            config = get_child(entry, "config")
            if config is not None and not isinstance(config, dict):
                return []
            systems = get_child(config, "oslist")
            beta = get_child(config, "betakey")
            arch = get_child(config, "osarch")
            # Validate every selector before filtering, even on an inactive entry.
            if any(selector is not None and not isinstance(selector, str) for selector in (systems, beta, arch)):
                return []
            if systems is not None:
                if "windows" not in {x.strip().casefold() for x in systems.split(",")}:
                    continue
            if beta is not None and beta.casefold() != branch.casefold():
                continue
            if arch is not None and arch != "64":
                continue
            executable = relative_executable(get_child(entry, "executable"))
            if executable is not None:
                result.append(executable)
        return result
    except (ValueError, OSError, RuntimeError, TypeError):
        return []
