"""Remember filesystem inputs that can change without a transcript changing."""

import stat
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_observations = ContextVar("path_observations", default=None)


@contextmanager
def observe_paths(entry):
    observed = {} if entry else None
    token = _observations.set(observed)
    try:
        yield observed
    finally:
        _observations.reset(token)


def _observe(operation, path, result):
    observed = _observations.get()
    if observed is not None:
        observed.setdefault((operation, path), set()).add(result)


def resolved(path):
    result = None
    try:
        result = str(path.resolve())
        return result
    finally:
        _observe("resolve", path, result)


def _special_file(path):
    try:
        return not stat.S_ISREG(Path(path).stat().st_mode)
    except FileNotFoundError:
        return path.startswith(("/dev/", "/proc/", "/sys/"))
    except OSError:
        return True


def special_file(path):
    result = _special_file(path)
    _observe("special", path, result)
    return result


def paths_unchanged(observed):
    for (operation, path), expected in observed.items():
        if operation == "resolve":
            try:
                result = resolved(path)
            except (OSError, RuntimeError):
                result = None
        else:
            result = _special_file(path)
        if expected != {result}:
            return False
    return True


def cached_parse(cache, entry):
    value = cache.get(entry) if entry else None
    if value is not None:
        parsed, observed = value
        if paths_unchanged(observed):
            return parsed
    return None
