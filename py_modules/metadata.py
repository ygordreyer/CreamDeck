"""Bounded HTTPS metadata only. No executable downloads or disk cache."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import ContextVar
import json
import os
import ssl
import time
import urllib.request
from safe_fs import app_id

MAX_RESPONSE = 2 * 1024 * 1024
MAX_NAMED_DLCS = 64
DEADLINE = 12
LOOKUPS = ThreadPoolExecutor(2, thread_name_prefix="creamdeck-metadata")
NAMES = ThreadPoolExecutor(8, thread_name_prefix="creamdeck-dlc-names")
EXPIRES = ContextVar("metadata_expires", default=None)
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
    expires = EXPIRES.get()
    timeout = min(10, expires - time.monotonic()) if expires is not None else 10
    if timeout <= 0:
        raise TimeoutError("Metadata deadline exceeded")
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls_context()))
    with opener.open(url, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise ValueError("Metadata response exceeds size limit")
        return json.loads(body)


def lookup(value):
    value = app_id(value)
    expires = time.monotonic() + DEADLINE
    token = EXPIRES.set(expires)
    try:
        return _lookup(value, expires)
    finally:
        EXPIRES.reset(token)


def _lookup(value, expires):
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
            entry = data.get(value, {})
            if not isinstance(entry.get("data"), dict) or entry.get("success") is False:
                raise ValueError("DLC metadata unavailable")
            ids = [app_id(str(x)) for x in entry["data"].get("dlc", [])]
        except (OSError, ValueError, TypeError, AttributeError):
            raise ValueError("DLC metadata unavailable") from None
    ids = sorted(set(ids))[:1000]
    def name_before_deadline(value):
        token = EXPIRES.set(expires)
        try:
            return dlc_name(value)
        finally:
            EXPIRES.reset(token)
    futures = [NAMES.submit(name_before_deadline, value) for value in ids[:MAX_NAMED_DLCS]]
    wait(futures, timeout=max(0, expires - time.monotonic()))
    names = [future.result() if future.done() and not future.cancelled() else None for future in futures]
    for future in futures:
        future.cancel()
    names += [None] * (len(ids) - len(names))
    return [{"id": x, "name": name or f"DLC {x}"} for x, name in zip(ids, names)]


def dlc_name(value):
    try:
        name = fetch(f"https://api.steamcmd.net/v1/info/{value}")["data"][value]["common"]["name"]
        return name.strip()[:120] or None if isinstance(name, str) else None
    except Exception:
        return None


async def dlcs(value, deadline=DEADLINE):
    app_id(value)
    try:
        result = await asyncio.wait_for(asyncio.wrap_future(LOOKUPS.submit(lookup, value)), deadline)
        return {"state": "ready", "dlcs": result}
    except (OSError, ValueError, TypeError, AttributeError, asyncio.TimeoutError):
        return {"state": "unavailable", "dlcs": []}
