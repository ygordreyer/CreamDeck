"""Bounded HTTPS metadata only. No executable downloads or disk cache."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import ssl
import urllib.request
from safe_fs import app_id

MAX_RESPONSE = 2 * 1024 * 1024
MAX_NAMED_DLCS = 64
# Decky's frozen Python looks for CAs under the build host's /usr/lib/ssl, which SteamOS lacks,
# so every HTTPS lookup failed verification and read as "no DLC".
CA_FILES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Metadata redirects are disabled")


def tls_context():
    context = ssl.create_default_context()
    for path in CA_FILES:
        if os.path.isfile(path):
            context.load_verify_locations(cafile=path)
            break
    return context


def fetch(url):
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls_context()))
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
    ids = sorted(set(ids))[:1000]
    with ThreadPoolExecutor(8) as pool:
        names = list(pool.map(dlc_name, ids[:MAX_NAMED_DLCS]))
    names += [None] * (len(ids) - len(names))
    return [{"id": x, "name": name or f"DLC {x}"} for x, name in zip(ids, names)]


def dlc_name(value):
    try:
        name = fetch(f"https://api.steamcmd.net/v1/info/{value}")["data"][value]["common"]["name"]
        return name.strip()[:120] or None if isinstance(name, str) else None
    except Exception:
        return None


async def dlcs(value):
    app_id(value)
    return await asyncio.to_thread(lookup, value)
