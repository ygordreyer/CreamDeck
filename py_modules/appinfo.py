"""Bounded, read-only access to one app's binary Steam launch configuration."""
from __future__ import annotations

import struct

from safe_fs import Directory, app_id
from vdf_lite import get_child

MAGIC = 0x07564429
MAX_SIZE = 64 * 1024 * 1024
MAX_STRING = 64 * 1024
MAX_COUNT = 1_000_000
MAX_DEPTH = 32
ENTRY_HEADER = 60
AMBIGUOUS = object()


class Reader:
    def __init__(self, data, start=0, end=None):
        self.data = data
        self.pos = start
        self.end = len(data) if end is None else end
        if not 0 <= start <= self.end <= len(data):
            raise ValueError("Invalid appinfo bounds")

    def take(self, size):
        if size < 0 or size > self.end - self.pos:
            raise ValueError("Truncated appinfo data")
        start = self.pos
        self.pos += size
        return self.data[start:self.pos]

    def number(self, fmt):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))[0]

    def string(self, wide=False):
        start = self.pos
        limit = min(self.end, start + MAX_STRING + (2 if wide else 1))
        if wide:
            while self.pos + 2 <= limit:
                if self.take(2) == b"\0\0":
                    break
            else:
                raise ValueError("Unterminated or oversized appinfo string")
            raw = self.data[start:self.pos - 2]
        else:
            end = self.data.find(b"\0", start, limit)
            if end < 0:
                raise ValueError("Unterminated or oversized appinfo string")
            raw = self.take(end - start)
            self.take(1)
        try:
            return raw.decode("utf-16le" if wide else "utf-8")
        except UnicodeError as error:
            raise ValueError("Invalid appinfo string encoding") from error


def pairs(reader, strings, depth=0, budget=None):
    if depth > MAX_DEPTH:
        raise ValueError("Appinfo nesting exceeds limit")
    budget = [MAX_COUNT] if budget is None else budget
    result = {}
    names = {}
    while True:
        kind = reader.number("B")
        if kind in (0x08, 0x0B):
            return result
        if kind not in (0, 1, 2, 3, 4, 5, 6, 7, 10):
            raise ValueError("Unknown appinfo value type")
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError("Too many appinfo values")
        index = reader.number("I")
        if index >= len(strings):
            raise ValueError("Appinfo string index out of range")
        key, folded = strings[index]
        if kind == 0:
            value = pairs(reader, strings, depth + 1, budget)
        elif kind in (1, 5):
            value = reader.string(wide=kind == 5)
        else:
            value = reader.number({2: "i", 3: "f", 4: "i", 6: "i", 7: "Q", 10: "q"}[kind])
        # Keep only the first spelling so case-insensitive lookup cannot bypass
        # ambiguity. The sentinel is neither a string nor a section.
        if folded in names:
            result[names[folded]] = AMBIGUOUS
        else:
            names[folded] = key
            result[key] = value


def launch_entries(cache_directory, value):
    value = int(app_id(value))
    try:
        with Directory(cache_directory) as directory:
            data = directory.read("appinfo.vdf", cap=MAX_SIZE)
    except (OSError, RuntimeError) as error:
        raise ValueError("Steam appinfo is unavailable") from error
    header = Reader(data)
    if header.number("I") != MAGIC:
        raise ValueError("Unsupported Steam appinfo format")
    header.number("I")
    offset = header.number("q")
    if offset < header.pos:
        raise ValueError("Invalid appinfo string table offset")
    table = Reader(data, offset)
    count = table.number("I")
    if count > MAX_COUNT or count > table.end - table.pos:
        raise ValueError("Invalid appinfo string count")
    strings = []
    for _ in range(count):
        key = table.string()
        # Duplicate grouping must use exactly the equivalence in get_child.
        strings.append((key, key.lower()))
    if table.pos != table.end:
        raise ValueError("Trailing appinfo string table data")
    entries = Reader(data, header.pos, offset)
    result = None
    while True:
        current = entries.number("I")
        if not current:
            if entries.pos != offset:
                raise ValueError("Trailing appinfo app list data")
            break
        size = entries.number("I")
        if size < ENTRY_HEADER or size > entries.end - entries.pos:
            raise ValueError("Invalid appinfo entry size")
        end = entries.pos + size
        if current == value:
            if result is not None:
                raise ValueError("Duplicate appinfo app ID")
            blob = Reader(data, entries.pos + ENTRY_HEADER, end)
            result = pairs(blob, strings)
            if blob.pos != end:
                raise ValueError("Trailing appinfo entry data")
        entries.pos = end
    if result is None:
        raise ValueError("App is missing from Steam appinfo")
    launches = get_child(get_child(get_child(result, "appinfo"), "config"), "launch")
    if not isinstance(launches, dict) or any(not isinstance(x, dict) for x in launches.values()):
        raise ValueError("Appinfo launch entries are unavailable")
    for entry in launches.values():
        if any(get_child(entry, key) is AMBIGUOUS for key in
               ("executable", "config", "oslist", "betakey", "osarch")):
            raise ValueError("Ambiguous appinfo launch entry")
        config = get_child(entry, "config")
        if any(get_child(config, key) is AMBIGUOUS for key in ("oslist", "betakey", "osarch")):
            raise ValueError("Ambiguous appinfo launch config")
    return list(launches.values())
