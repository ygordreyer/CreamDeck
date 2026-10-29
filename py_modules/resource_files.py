"""Bundled SmokeAPI pins identify the reviewed snapshot, not upstream authenticity."""
import os
from safe_fs import Directory, digest

PINS = {
    "steam_api.dll": "145aadfffde5140991995d76dca8f2423e9d7f9dbb66bc21565cee59746f027b",
    "steam_api64.dll": "3891cd70b8e06cad474b9ea5b6d7c3d19b9948c41df2495e3cf02d2a256498ef",
}


def verified_resources():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "SmokeAPI")
    with Directory(path) as directory:
        result = {name: directory.read(name) for name in PINS}
    if any(digest(data) != PINS[name] for name, data in result.items()):
        raise ValueError("SmokeAPI resource hash mismatch. Refusing install")
    return result
