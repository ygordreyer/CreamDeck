from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "py_modules"))

import decky
import metadata
import steam_library
from install_state import LOCK, Store
from smokeapi import Installer


def require_user():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError("CreamDeck Hardened refuses to run as root")


def backend():
    require_user()
    return Installer(Store(decky.DECKY_PLUGIN_SETTINGS_DIR), steam_library.find_game)


class Plugin:
    async def get_game_page(self, app_id: str):
        def read():
            with LOCK:
                instance = backend()
                game = steam_library.find_game(app_id)
                status, record = instance.status(app_id)
                return {"found": True, "name": game["name"], "status": status,
                        "allowed": app_id in instance.store.allowlist(), "record": record}
        try:
            result = await asyncio.to_thread(read)
            result["dlcs"] = await metadata.dlcs(app_id)
            return result
        except Exception as error:
            return {"found": False, "error": str(error)}

    async def allow_game(self, app_id: str, confirmed: bool):
        def allow():
            steam_library.find_game(app_id)
            backend().store.allow(app_id, confirmed)
        return await self._operation(allow)

    async def acknowledge_launch_restore(self, app_id: str):
        return await self._operation(lambda: {"record": backend().resolve_launch(app_id)})

    async def resolve_launch_by_hand(self, app_id: str, confirmed: bool):
        def resolve():
            if confirmed is not True:
                raise ValueError("Confirm that original launch options were restored by hand")
            return {"record": backend().resolve_launch(app_id, manual=True)}
        return await self._operation(resolve)

    async def install_smokeapi(self, app_id: str, mode: str, proxy: str,
                               enabled_dlc_ids: list[str], original_launch_options: str,
                               manual_launch: bool = False):
        return await self._operation(lambda: {"record": backend().install(
            app_id, mode, proxy, enabled_dlc_ids, original_launch_options, manual_launch)})

    async def uninstall_smokeapi(self, app_id: str):
        def remove():
            record, warnings = backend().uninstall(app_id)
            return {"record": record, "warnings": warnings}
        return await self._operation(remove)

    async def _operation(self, function):
        try:
            require_user()
            result = await asyncio.to_thread(function)
            return {"success": True, **(result or {})}
        except Exception as error:
            decky.logger.error("CreamDeck Hardened operation refused: %s", error)
            return {"success": False, "error": str(error)}

    async def _main(self):
        require_user()
        decky.logger.info("CreamDeck Hardened loaded as the Decky user")

    async def _unload(self):
        pass

    async def _uninstall(self):
        decky.logger.info("Remove game files and restore launch options before removing the plugin")
