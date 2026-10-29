"""Bounded HTTPS metadata only. No executable downloads or disk cache."""
import asyncio
import json
import ssl
import urllib.request
from safe_fs import app_id

MAX_RESPONSE = 2 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Metadata redirects are disabled")


def fetch(url):
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(url, timeout=10) as response:
        body = response.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise ValueError("Metadata response exceeds size limit")
        return json.loads(body)


def lookup(value):
    value = app_id(value)
    ids = []
    try:
        data = fetch(f"https://api.steamcmd.net/v1/info/{value}")
        entry = data.get("data", {}).get(value, {})
        raw = entry.get("extended", {}).get("listofdlc", "")
        if isinstance(raw, str):
            ids = [app_id(x.strip()) for x in raw.split(",") if x.strip()]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    if not ids:
        try:
            data = fetch(f"https://store.steampowered.com/api/appdetails?appids={value}&filters=basic")
            ids = [app_id(str(x)) for x in data.get(value, {}).get("data", {}).get("dlc", [])]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return [{"id": x, "name": f"DLC {x}"} for x in sorted(set(ids))[:1000]]


async def dlcs(value):
    app_id(value)
    return await asyncio.to_thread(lookup, value)
