"""Windows-only test adapter. Production always requires Linux file primitives.

The transaction tests run on real temporary files through this adapter on
Windows. On Linux they run directly through safe_fs.Directory instead.
This adapter does not claim to test Linux descriptor or renameat2 behavior.
"""
import os
from pathlib import Path
import stat
import uuid
from safe_fs import Directory, component, digest


class WindowsTestDirectory(Directory):
    def __init__(self, path):
        self.path = Path(path)
        for part in [self.path, *self.path.parents]:
            if part.is_symlink():
                raise ValueError("Symlink directory")
        if not self.path.exists():
            raise FileNotFoundError(str(self.path))
        if not self.path.is_absolute() or not self.path.is_dir():
            raise ValueError("Expected existing absolute directory")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def names(self):
        return os.listdir(self.path)

    def entries(self):
        return [(x.name, x.lstat()) for x in self.path.iterdir()]

    def child(self, name):
        return type(self)(str(self.path / component(name)))

    def parent(self):
        return type(self)(str(self.path.parent))

    def ensure_child_dir(self, name):
        path = self.path / component(name)
        path.mkdir(exist_ok=True)
        with self.child(name):
            pass

    def exists(self, name):
        path = self.path / component(name)
        if path.is_symlink():
            raise ValueError("Refusing symlink file")
        if not path.exists():
            return False
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Refusing non-regular file")
        return True

    def read(self, name, cap=16 * 1024 * 1024):
        if not self.exists(name):
            raise FileNotFoundError(name)
        data = (self.path / name).read_bytes()
        if len(data) > cap:
            raise ValueError("Size cap")
        return data

    def read_head(self, name, cap):
        if not isinstance(cap, int) or cap < 0:
            raise ValueError("Invalid head read size")
        if not self.exists(name):
            raise FileNotFoundError(name)
        with (self.path / name).open("rb") as stream:
            return stream.read(cap)

    def hash(self, name):
        return digest(self.read(name)) if self.exists(name) else None

    def write_new(self, name, data):
        with (self.path / component(name)).open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self.sync()

    def raw_rename(self, source, target):
        os.rename(self.path / component(source), self.path / component(target))
        self.sync()

    def identity(self):
        info = self.path.stat()
        return info.st_dev, info.st_ino

    def move_to(self, source, destination, target):
        os.rename(self.path / component(source), destination.path / component(target))

    def raw_delete(self, name):
        (self.path / component(name)).unlink()
        self.sync()

    def create_probe(self, name):
        (self.path / component(name)).mkdir()

    def remove_probe(self, name):
        (self.path / component(name)).rmdir()

    def remove_probe_file(self, name):
        (self.path / component(name)).unlink()

    def filesystem_name(self):
        return "testfs"

    def sync(self):
        pass

    def atomic_json(self, name, data):
        temp = ".cdh-" + uuid.uuid4().hex
        self.exists(name)
        try:
            self.write_new(temp, data)
            self.exists(name)
            os.replace(self.path / temp, self.path / name)
        finally:
            if self.exists(temp):
                self.delete(temp, self.hash(temp))


TestDirectory = WindowsTestDirectory if os.name == "nt" else Directory
