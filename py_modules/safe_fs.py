"""Linux descriptor-relative file operations. Unsupported hosts fail closed."""
from __future__ import annotations

import hashlib
import ctypes
import errno
import os
import platform
import re
import stat
import uuid


def app_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", value):
        raise ValueError("App ID must be a positive decimal string")
    return value


def component(value: str) -> str:
    if (not isinstance(value, str) or not value or value in (".", "..")
            or ".." in value or any(c in value for c in '/\\:\x00')
            or os.path.isabs(value)):
        raise ValueError("Expected a single safe path component")
    return value


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rename_noreplace(fd, source, target, target_fd=None):
    libc = ctypes.CDLL(None, use_errno=True)
    args = (ctypes.c_int(fd), ctypes.c_char_p(os.fsencode(component(source))),
            ctypes.c_int(fd if target_fd is None else target_fd),
            ctypes.c_char_p(os.fsencode(component(target))), ctypes.c_uint(1))
    wrapper = getattr(libc, "renameat2", None)
    if wrapper is not None:
        result = wrapper(*args)
    else:
        number = {"x86_64": 316, "aarch64": 276}.get(platform.machine().lower())
        if number is None:
            raise RuntimeError("renameat2 syscall unavailable on this architecture: " + platform.machine())
        result = libc.syscall(ctypes.c_long(number), *args)
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


class Directory:
    def __init__(self, path: str):
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            raise RuntimeError("Safe file operations require Linux O_NOFOLLOW and dir_fd")
        if not os.path.isabs(path):
            raise ValueError("Directory must be absolute")
        # Walk from / so a changed ancestor cannot redirect the final open.
        self.fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in path.split("/"):
                if not part:
                    continue
                component(part)
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
                os.close(self.fd)
                self.fd = new
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def names(self):
        return os.listdir(self.fd)

    def entries(self):
        return [(name, os.stat(name, dir_fd=self.fd, follow_symlinks=False)) for name in self.names()]

    def child(self, name):
        child = object.__new__(Directory)
        child.fd = os.open(component(name), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        return child

    def ensure_child_dir(self, name):
        component(name)
        try:
            os.mkdir(name, mode=0o700, dir_fd=self.fd)
            self.sync()
        except FileExistsError:
            pass
        with self.child(name):
            pass

    def exists(self, name):
        component(name)
        try:
            info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Refusing symlink or non-regular file: {name}")
        return True

    def read(self, name, cap=16 * 1024 * 1024):
        component(name)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"Not a regular file: {name}")
            data = stream.read(cap + 1)
            if len(data) > cap:
                raise ValueError(f"File exceeds size limit: {name}")
            return data

    def read_head(self, name, cap):
        component(name)
        if not isinstance(cap, int) or cap < 0:
            raise ValueError("Invalid head read size")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"Not a regular file: {name}")
            return stream.read(cap)

    def hash(self, name):
        return digest(self.read(name)) if self.exists(name) else None

    def write_new(self, name, data):
        component(name)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        # Preserve partial writes for journaled recovery. Probe cleanup owns
        # its exclusive directory and removes partial writes independently.
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self.sync()

    def identity(self):
        info = os.fstat(self.fd)
        return info.st_dev, info.st_ino

    def move_to(self, source, destination, target):
        # No synchronization here: callers must distinguish failed rename
        # from a successful rename followed by failed synchronization.
        rename_noreplace(self.fd, source, target, destination.fd)

    def raw_rename(self, source, target):
        rename_noreplace(self.fd, source, target)
        self.sync()

    def raw_delete(self, name):
        os.unlink(component(name), dir_fd=self.fd)
        self.sync()

    def bind_journal(self, record, save):
        self.quarantine = record["quarantine"]
        self.save_journal = lambda: save(record)

    def _forget(self, name):
        del self.quarantine[name]
        self.save_journal()

    def _resolve(self, name, entry):
        source, target, expected = entry["source"], entry["target"], entry["expected"]
        if "dev" not in entry or "ino" not in entry:
            raise ValueError(f"Unowned legacy quarantine preserved at {name}; inspect manually")
        try:
            capture = self.child(name)
        except FileNotFoundError:
            self._forget(name)
            return
        changed = False
        with capture:
            if capture.identity() != (entry["dev"], entry["ino"]):
                raise ValueError(f"Quarantine directory identity changed: {name}; preserved for inspection")
            if capture.exists("capture"):
                changed = capture.hash("capture") != expected
                destination = source if changed else target
                if destination is None:
                    capture.raw_delete("capture")
                else:
                    try:
                        capture.move_to("capture", self, destination)
                    except FileExistsError:
                        entry["warning"] = f"Captured file preserved in {name}/capture; final name {destination} is occupied"
                        self.save_journal()
                        raise ValueError(entry["warning"])
                    capture.sync()
                    self.sync()
        self.remove_probe(name)
        self.sync()
        self._forget(name)
        if changed:
            raise ValueError(f"Changed file preserved at {source} (captured in {name})")

    def recover_quarantine(self):
        warnings = []
        for name, entry in list(self.quarantine.items()):
            try:
                self._resolve(name, entry)
            except ValueError as error:
                warnings.append(str(error))
        return warnings

    def _capture(self, source, target, expected):
        component(source)
        if target is not None:
            component(target)
        if not hasattr(self, "quarantine"):
            self.quarantine, self.save_journal = {}, lambda: None
        name = ".cdh-q-" + uuid.uuid4().hex
        # Exclusive mkdir precedes intent journaling. Only this directory's
        # recorded identity authorizes recovery, never a pathname alone.
        self.create_probe(name)
        with self.child(name) as capture:
            dev, ino = capture.identity()
            try:
                self.sync()
            except BaseException:
                self.remove_probe(name)
                raise
            self.quarantine[name] = {"source": source, "target": target, "expected": expected,
                                     "warning": "", "dev": dev, "ino": ino}
            try:
                self.save_journal()
            except BaseException:
                del self.quarantine[name]
                self.remove_probe(name)
                raise
            try:
                self.move_to(source, capture, "capture")
            except OSError:
                # A failed rename owns no file. Never resolve an occupied
                # destination as a capture, even if its bytes match.
                self._forget(name)
                if not capture.names():
                    self.remove_probe(name)
                    self.sync()
                raise
            capture.sync()
            self.sync()
        self._resolve(name, self.quarantine[name])

    def rename(self, source, target, expected):
        self._capture(source, target, expected)

    def delete(self, name, expected):
        self._capture(name, None, expected)

    def filesystem_name(self):
        try:
            path = os.readlink(f"/proc/self/fd/{self.fd}")
            matches = []
            with open("/proc/mounts", encoding="utf-8") as mounts:
                for line in mounts:
                    fields = line.split()
                    mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[1])
                    if path == mount or path.startswith(mount.rstrip("/") + "/"):
                        matches.append((len(mount), fields[2]))
            return max(matches)[1] if matches else "unknown filesystem"
        except (OSError, ValueError, IndexError):
            return "unknown filesystem"

    def rename_preflight(self):
        try:
            self._rename_preflight()
        except OSError as error:
            if error.errno in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
                raise RuntimeError(f"Incompatible filesystem {self.filesystem_name()}: "
                                   "RENAME_NOREPLACE or durable directory operations are unsupported") from error
            raise

    def _rename_preflight(self):
        name = ".cdh-probe-" + uuid.uuid4().hex
        self.create_probe(name)
        failed = False
        try:
            with self.child(name) as probe:
                try:
                    probe.write_new("source", b"renameat2 probe")
                    probe.write_new("occupied", b"collision probe")
                    try:
                        probe.raw_rename("source", "occupied")
                    except FileExistsError:
                        pass
                    else:
                        raise RuntimeError("renameat2 did not honor no-replace")
                    probe.raw_rename("source", "target")
                except BaseException:
                    failed = True
                    raise
                finally:
                    # This directory is exclusively ours. Do not use the
                    # rename or fsync capabilities being tested to clean it.
                    try:
                        entries = probe.names()
                    except OSError:
                        if not failed:
                            raise
                        entries = []
                    for entry in entries:
                        try:
                            probe.remove_probe_file(entry)
                        except OSError:
                            if not failed:
                                raise
        except BaseException:
            failed = True
            raise
        finally:
            try:
                self.remove_probe(name)
            except OSError:
                if not failed:
                    raise

    def create_probe(self, name):
        os.mkdir(component(name), mode=0o700, dir_fd=self.fd)

    def remove_probe(self, name):
        os.rmdir(component(name), dir_fd=self.fd)

    def remove_probe_file(self, name):
        os.unlink(component(name), dir_fd=self.fd)

    def atomic_json(self, name, data):
        temp = ".cdh-" + uuid.uuid4().hex
        self.exists(name)
        try:
            self.write_new(temp, data)
            self.exists(name)
            os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            self.sync()
        finally:
            if self.exists(temp):
                self.delete(temp, self.hash(temp))

    def sync(self):
        os.fsync(self.fd)
