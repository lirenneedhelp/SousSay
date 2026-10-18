import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Configuration must exist before `main` is imported (it refuses to start without it).
TEST_ENV = {
    "APP_ENV": "development", "ACCESS_CODE": "test-access-code-123", "SESSION_SECRET": "s" * 40,
    "MCP_API_KEY": "mcp-key-for-tests-0123456789abcdef", "ALLOWED_HOSTS": "testserver,localhost,127.0.0.1",
    "COOKIE_SECURE": "false",
}
os.environ.update(TEST_ENV)

import cooking  # noqa: E402
import db  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from security import COOKIE_NAME, issue_cookie  # noqa: E402

SID = "session-test-1"
TEST_USER = "testclient"


def make_client(user: str | None = TEST_USER) -> TestClient:
    """A TestClient signed in as `user` (a forged-but-validly-signed cookie), or anonymous if None."""
    client = TestClient(main.app)
    if user:
        client.headers["Cookie"] = f"{COOKIE_NAME}={issue_cookie(main.settings, user)}"
    return client


@pytest.fixture(autouse=True)
def principal():
    """Service-layer tests run as the same principal the default test client is signed in as."""
    with cooking.acting_as(f"u:{TEST_USER}"):
        yield


@pytest.fixture(autouse=True)
def fresh_limits():
    main.limiter.clear()


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TALKSOUS_DB", str(tmp_path / "test.db"))
    db.init_db()


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(cooking, "_now", lambda: c.t)
    return c


class FakeBedrock:
    """Scripted stand-in for bedrock.converse. Items are assistant messages or Exceptions."""

    def __init__(self, script, repeat_last=False):
        self.script, self.calls, self.repeat_last = list(script), [], repeat_last

    def converse(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        item = self.script[0] if (self.repeat_last and len(self.script) == 1) else self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return {"output": {"message": item}}


def text(t):
    return {"role": "assistant", "content": [{"text": t}]}


def tools(*uses):
    return {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": f"use{i}", "name": n, "input": a}} for i, (n, a) in enumerate(uses)]}
