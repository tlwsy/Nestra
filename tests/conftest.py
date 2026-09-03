"""共享 fixture。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nestra.core.crypto import Crypto
from nestra.storage.db import Database

TEST_SECRET = "test-secret-key-do-not-use-in-production-0123456789"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离宿主机环境变量。

    否则开发机上真实的 NESTRA__* / *_API_KEY 会让测试结果随机器变化。
    """
    for key in list(os.environ):
        if key.startswith(("NESTRA_", "NESTRA__")) or key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NESTRA_SECRET_KEY", TEST_SECRET)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db", cache_mb=4)
    database.migrate()
    return database


@pytest.fixture
def crypto() -> Crypto:
    return Crypto(TEST_SECRET)


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
