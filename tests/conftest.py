import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cooking  # noqa: E402
import db  # noqa: E402

SID = "session-test-1"


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
