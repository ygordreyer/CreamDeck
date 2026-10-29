# SmokeAPI snapshot provenance

These bundled files are copied from upstream Qiming-Liu/CreamDeck commit `242a6147eb56b4a17c08e859c22f10e456387ed8`.

| File | SHA256 |
|---|---|
| steam_api.dll | 145aadfffde5140991995d76dca8f2423e9d7f9dbb66bc21565cee59746f027b |
| steam_api64.dll | 3891cd70b8e06cad474b9ea5b6d7c3d19b9948c41df2495e3cf02d2a256498ef |

Both files are byte-identical to the official acidicoala/SmokeAPI release v4.1.3 (checked 2026-10-28):
- Asset: https://github.com/acidicoala/SmokeAPI/releases/download/v4.1.3/SmokeAPI-v4.1.3.zip
- Asset digest: sha256:8a443b5b4c434a4904b272ea4d93ad7e13e212626c0e49466176cb828c1d2a3b
- steam_api.dll is the release's smoke_api32.dll; steam_api64.dll is its smoke_api64.dll.

This establishes origin only. It does not audit the native code. Native DLL network behavior, credential access and configuration compatibility remain unverified.
The Python installer verifies both hashes before copying either file.
