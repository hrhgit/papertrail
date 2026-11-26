import pytest


@pytest.fixture(scope="session", autouse=True)
def session_parse_cache(tmp_path_factory):
    # Module fixtures can run before function-scoped fixtures are entered.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PAPERTRAIL_CACHE", str(tmp_path_factory.mktemp("session-parse-cache")))
        yield


@pytest.fixture(autouse=True)
def private_parse_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERTRAIL_CACHE", str(tmp_path / "parse-cache"))
