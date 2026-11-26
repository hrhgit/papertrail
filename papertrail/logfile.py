"""Open agent logs, including Codex rollouts that Codex has compressed with zstd.

Codex keeps recent rollouts as `rollout-*.jsonl` and later rewrites older ones as
`rollout-*.jsonl.zst`. Paper Trail adds no dependency for this. It uses the first
decoder it finds: the standard library's `compression.zstd` (Python 3.14+), the
`zstandard` package, or the `zstd` command.
"""

import io
import shutil
import subprocess
from pathlib import Path

ZST = ".zst"


class ZstdUnavailable(OSError):
    """No zstd decoder is available on this machine."""


def compressed(path):
    return str(path).endswith(ZST)


def log_stem(path):
    """The file name without `.jsonl` or `.jsonl.zst`."""
    name = Path(path).name
    for suffix in (".jsonl" + ZST, ".jsonl"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(path).stem


def current(path):
    """The log as it is now: Codex may have compressed `x.jsonl` to `x.jsonl.zst`
    since the graph was built."""
    path = Path(path)
    if not path.is_file() and not compressed(path):
        packed = path.with_name(path.name + ZST)
        if packed.is_file():
            return packed
    return path


def _stdlib():
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        return None
    return zstd


def _package():
    try:
        import zstandard
    except ImportError:
        return None
    return zstandard


def zstd_available():
    return bool(_stdlib() or _package() or shutil.which("zstd"))


class _Command(io.RawIOBase):
    """A readable stream over `zstd -dc PATH` that reports a failed decode."""

    def __init__(self, path):
        self.proc = subprocess.Popen(
            ["zstd", "-qdc", "--", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def readable(self):
        return True

    def readinto(self, buffer):
        n = self.proc.stdout.readinto(buffer)
        if n == 0 and self.proc.wait() != 0:
            raise OSError(f"zstd could not read the file (exit {self.proc.returncode})")
        return n

    def close(self):
        if not self.closed:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.stdout.close()
            self.proc.wait()
        super().close()


class _PackageStream(io.RawIOBase):
    """Read all frames, checking that input ends at a frame boundary."""

    def __init__(self, path, zstandard):
        self.raw = None
        self.decompressor = zstandard.ZstdDecompressor()
        self.frame = self.decompressor.decompressobj()
        self.raw = path.open("rb")
        self.pending = b""
        self.decoded = memoryview(b"")

    def readable(self):
        return True

    def readinto(self, buffer):
        while not self.decoded:
            data = self.pending or self.raw.read(io.DEFAULT_BUFFER_SIZE)
            if not data:
                if not self.frame.eof:
                    raise EOFError("Compressed log ended before the end of a zstd frame")
                return 0
            if self.frame.eof:
                self.frame = self.decompressor.decompressobj()
            self.decoded = memoryview(self.frame.decompress(data))
            self.pending = self.frame.unused_data
        n = min(len(buffer), len(self.decoded))
        buffer[:n] = self.decoded[:n]
        self.decoded = self.decoded[n:]
        return n

    def close(self):
        if not self.closed and self.raw is not None:
            self.raw.close()
        super().close()


def open_log(path):
    """A binary stream of the log's JSONL text. Raises OSError when it can't be read,
    and ZstdUnavailable when the file is compressed and no decoder exists."""
    path = Path(path)
    if not compressed(path):
        return path.open("rb")
    if zstd := _stdlib():
        return zstd.open(path, "rb")
    if zstandard := _package():
        return io.BufferedReader(_PackageStream(path, zstandard))
    if shutil.which("zstd"):
        return io.BufferedReader(_Command(path))
    raise ZstdUnavailable(f"no zstd decoder for {path.name}")
