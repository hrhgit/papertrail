"""Private, disposable parse results. Each entry is published atomically."""

import hashlib
import json
import os
import pickle
import stat
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile


@lru_cache(maxsize=1)
def parser_version():
    package = Path(__file__).parent
    files = [package / name for name in ("model.py", "logfile.py", "patches.py", "fragments.py")]
    files.extend(sorted((package / "sources").glob("*.py")))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


# Fingerprint the source at import, before an installed reader can outlive an
# edit to its source files. If source is unavailable, leave caching disabled.
try:
    PARSER_VERSION = parser_version()
except OSError:
    PARSER_VERSION = None


def stamp(path):
    try:
        info = path.stat()
        return str(path.resolve()), info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino
    except (OSError, RuntimeError):
        return None


class ParseCache:
    def __init__(self, *, enabled=True, project=None):
        self.directory = None
        if not enabled or PARSER_VERSION is None:
            return
        try:
            path = Path(os.environ.get("PAPERTRAIL_CACHE") or "~/.cache/papertrail").expanduser().resolve()
            if project is not None and path.is_relative_to(Path(project).resolve()):
                return
            if any(
                (folder / marker).exists()
                for folder in (path, *path.parents)
                for marker in (".git", ".hg", ".svn")
            ):
                return
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = path.stat()
            if info.st_uid == os.getuid() and not info.st_mode & 0o077:
                self.version = PARSER_VERSION
                self.directory = path
        except (OSError, RuntimeError, ValueError):
            pass  # Cache access never prevents an ingest.

    def _target(self, identity):
        digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        return self.directory / (digest + ".cache")

    def entry(self, name, paths, *, kind="parse", stamps=None, sidecars=(), context=()):
        if self.directory is None:
            return None
        stamps = tuple(map(stamp, paths)) if stamps is None else stamps
        if any(value is None for value in stamps):
            return None
        # Keep the spelling too: it determines the session ID, log location and
        # relative paths when several symlinks name the same physical transcript.
        identity = [name, kind, [str(path) for path in paths]]
        header = json.dumps([identity, self.version, stamps, list(map(stamp, sidecars)), context])
        return self._target(identity), header.encode()

    def _open(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OSError("Cache entry is not private")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def get(self, entry, *, probe=False):
        if entry is not None:
            path, header = entry
            try:
                with self._open(path) as stream:
                    if stream.readline().rstrip(b"\n") == header:
                        checksum = stream.readline().rstrip(b"\n")
                        if probe:
                            return len(checksum) == 64 and bool(stream.peek(1))
                        payload = stream.read()
                        if hashlib.sha256(payload).hexdigest().encode() == checksum:
                            return pickle.loads(payload)
            except Exception:
                # Unreadable, truncated and incompatible entries are all misses.
                pass
        return None

    def put(self, entry, value):
        if entry is None:
            return
        target, header = entry
        temporary = None
        try:
            with NamedTemporaryFile(dir=self.directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(header + b"\n")
                payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
                stream.write(hashlib.sha256(payload).hexdigest().encode() + b"\n")
                stream.write(payload)
            os.replace(temporary, target)
        except (OSError, pickle.PickleError, RecursionError):
            pass
        finally:
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def prune(self):
        if self.directory is None:
            return
        try:
            for path in self.directory.glob("*.cache"):
                try:
                    with self._open(path) as stream:
                        identity, *_ = json.loads(stream.readline())
                    if path == self._target(identity) and any(not Path(log).exists() for log in identity[2]):
                        path.unlink(missing_ok=True)
                except (OSError, ValueError, TypeError, IndexError):
                    continue
        except OSError:
            pass
