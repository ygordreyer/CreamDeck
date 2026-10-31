from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "py_modules"))

import decky
import metadata
import steam_library
from install_state import LOCK, Store
from safe_fs import app_id as validated_app_id
from smokeapi import Installer, available_proxies

MAX_GAME_NAME_LENGTH = 256


def require_user():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError("CreamDeck Hardened refuses to run as root")


def backend():
    require_user()
    return Installer(Store(decky.DECKY_PLUGIN_SETTINGS_DIR), steam_library.find_game,
                     steam_library.launch_executables)


class Plugin:
    async def get_game_page(self, app_id: str):
        def read():
            with LOCK:
                instance = backend()
                game = steam_library.find_game(app_id)
                status, record = instance.status(app_id)
                return {"found": True, "name": game["name"], "status": status,
                        "allowed": app_id in instance.store.allowlist(), "record": record,
                        "dlcs": [{"id": value, "name": f"DLC {value}"} for value in (record or {}).get("enabled_dlc_ids", [])],
                        "available_proxies": available_proxies(game["game_directory"])}
        try:
            return await asyncio.to_thread(read)
        except Exception as error:
            return {"found": False, "error": str(error)}

    async def get_game_metadata(self, app_id: str):
        require_user()
        return await metadata.dlcs(app_id)

    async def get_library_availability(self):
        require_user()
        _, unavailable = await asyncio.to_thread(steam_library.library_availability)
        return {"unavailable": unavailable}

    async def get_recorded_games(self):
        def read():
            # read_records returns a detached snapshot under LOCK; manifests need no lock.
            records = Store.read_records(decky.DECKY_PLUGIN_SETTINGS_DIR)
            games = []
            for record in records:
                if record["phase"] == "removed" and record["launch_restore"] != "pending":
                    continue
                value = record["app_id"]
                try:
                    name = steam_library.find_game(value).get("name")
                except (ValueError, OSError):
                    name = None
                name = name.strip() if isinstance(name, str) else ""
                if not name or len(name) > MAX_GAME_NAME_LENGTH:
                    name = f"Steam app {value}"
                state = ("Restoration pending" if record["launch_restore"] == "pending"
                         else "Recovery needed" if record["phase"] != "installed" or record.get("config_update")
                         else "Setup pending" if record.get("launch_setup") == "pending"
                         else "Installed")
                games.append({"app_id": value, "name": name, "state": state})
            return {"games": games}
        try:
            require_user()
            return await asyncio.to_thread(read)
        except Exception:
            return {"games": [], "error": "Recorded games unavailable"}

    async def save_dlc_selection(self, app_id: str, enabled_dlc_ids: list[str]):
        return await self._operation(app_id, lambda: {"record": backend().save_dlc_selection(app_id, enabled_dlc_ids)},
                                     operation="save_dlc_selection")

    async def acknowledge_launch_setup(self, app_id: str, manual: bool = False):
        return await self._operation(app_id, lambda: {"record": backend().resolve_setup(app_id, manual)},
                                     operation="acknowledge_launch_setup")

    async def allow_game(self, app_id: str, confirmed: bool):
        def allow():
            steam_library.find_game(app_id)
            backend().store.allow(app_id, confirmed)
        return await self._operation(app_id, allow, operation="allow_game")

    async def acknowledge_launch_restore(self, app_id: str):
        return await self._operation(app_id, lambda: {"record": backend().resolve_launch(app_id)},
                                     operation="acknowledge_launch_restore")

    async def resolve_launch_by_hand(self, app_id: str, confirmed: bool):
        def resolve():
            if confirmed is not True:
                raise ValueError("Confirm that original launch options were restored by hand")
            return {"record": backend().resolve_launch(app_id, manual=True)}
        return await self._operation(app_id, resolve, operation="resolve_launch_by_hand")

    async def install_smokeapi(self, app_id: str, mode: str, proxy: str,
                               enabled_dlc_ids: list[str], original_launch_options: str,
                               manual_launch: bool = False):
        return await self._operation(app_id, lambda: {"record": backend().install(
            app_id, mode, proxy, enabled_dlc_ids, original_launch_options, manual_launch)},
                                     operation="install_smokeapi")

    async def uninstall_smokeapi(self, app_id: str):
        def remove():
            record, warnings = backend().uninstall(app_id)
            return {"record": record, "warnings": warnings}
        return await self._operation(app_id, remove, operation="uninstall_smokeapi")

    async def _operation(self, app_id, function, operation="operation"):
        try:
            require_user()
            result = await asyncio.to_thread(function)
            return {"success": True, **(result or {})}
        except Exception as error:
            try:
                log_app_id = validated_app_id(app_id)
            except ValueError:
                log_app_id = "invalid"
            category = ("filesystem error" if isinstance(error, OSError)
                        else "validation" if isinstance(error, ValueError) else "refused")
            decky.logger.error("CreamDeck Hardened %s refused for app %s: %s", operation, log_app_id, category)
            return {"success": False, "error": str(error)}

    async def _main(self):
        require_user()
        decky.logger.info("CreamDeck Hardened loaded as the Decky user")

    async def _unload(self):
        pass

    async def _uninstall(self):
        decky.logger.info("Remove game files and restore launch options before removing the plugin")
