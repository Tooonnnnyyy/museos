from __future__ import annotations

import os

import pytest

from museos.config import Settings
from museos.gateway.base import ProviderConfig
from museos.gateway.mock import MockProvider
from museos.gateway.registry import ProviderRegistry
from museos.orchestrator import Orchestrator
from museos.store import Store

# SQLite is used only for tests (documented in README). Set MUSEOS_TEST_DATABASE_URL
# to run test_postgres.py against a real PostgreSQL database.


def mock_cfg(pid: str, caps: set[str] | None = None, **mock) -> ProviderConfig:
    return ProviderConfig(id=pid, adapter="mock", model=f"{pid}-model",
                          capabilities=caps or {"reasoning", "planning", "json", "review", "synthesis"},
                          cost_per_1k_input=0.001, cost_per_1k_output=0.002, mock=mock)


@pytest.fixture(autouse=True)
def _reset_mock_counters():
    MockProvider.reset_counters()
    yield


@pytest.fixture
async def store(tmp_path):
    s = Store(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await s.init()
    yield s
    await s.close()


@pytest.fixture
def make_orch(store):
    created: list[Orchestrator] = []

    def _make(configs: list[ProviderConfig], **registry_kw) -> Orchestrator:
        reg = ProviderRegistry.from_configs(configs, **registry_kw)
        orch = Orchestrator(Settings(database_url="unused", retry_backoff_s=0.0), reg, store)
        created.append(orch)
        return orch

    yield _make


@pytest.fixture
def pg_url():
    url = os.environ.get("MUSEOS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("MUSEOS_TEST_DATABASE_URL not set")
    return url
