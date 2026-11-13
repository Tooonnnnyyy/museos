"""Signup, login, logout, and per-user data isolation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swarmos.api import create_app
from swarmos.config import Settings
from swarmos.gateway.registry import ProviderRegistry
from tests.conftest import mock_cfg


@pytest.fixture
def client(tmp_path):
    cfgs = [mock_cfg("alpha"), mock_cfg("beta")]
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}", retry_backoff_s=0,
                        allowed_hosts=["testserver"], auth_enabled=True)
    with TestClient(create_app(settings, ProviderRegistry.from_configs(cfgs))) as c:
        yield c


def signup(client: TestClient, email: str, password: str = "supersecret1"):
    r = client.post("/auth/signup", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r


def test_unauthenticated_gets_401_and_redirects(client):
    assert client.get("/runs").status_code == 401
    assert client.get("/analytics/summary").status_code == 401
    assert client.get("/admin/summary").status_code == 401
    assert client.get("/providers").status_code == 401
    assert client.post("/runs", json={"idea": "x"}).status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/login"
    assert client.get("/login").status_code == 200
    # health stays public (liveness only, no data)
    assert client.get("/health").status_code == 200


def test_signup_login_logout_me(client):
    signup(client, "a@example.com")
    me = client.get("/auth/me").json()
    assert me["user"]["email"] == "a@example.com"
    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401

    r = client.post("/auth/login", json={"email": "a@example.com", "password": "supersecret1"})
    assert r.status_code == 200
    assert client.get("/auth/me").json()["user"]["email"] == "a@example.com"

    client.post("/auth/logout")
    assert client.post("/auth/login", json={"email": "a@example.com", "password": "wrongpass1"}).status_code == 401


def test_duplicate_email_and_validation(client):
    signup(client, "dup@example.com")
    assert client.post("/auth/signup", json={"email": "DUP@example.com",
                                             "password": "supersecret1"}).status_code == 409
    assert client.post("/auth/signup", json={"email": "not-an-email",
                                             "password": "supersecret1"}).status_code == 422
    assert client.post("/auth/signup", json={"email": "short@example.com",
                                             "password": "123"}).status_code == 422


def test_user_isolation(client):
    signup(client, "alice@example.com")
    r = client.post("/runs?wait=true", json={"idea": "Alice private idea", "max_tasks": 2}).json()
    alice_run = r["run_id"]
    assert client.get("/runs").json()[0]["run_id"] == alice_run
    client.post("/auth/logout")

    signup(client, "bob@example.com")
    assert client.get("/runs").json() == []
    assert client.get(f"/runs/{alice_run}").status_code == 404
    assert client.get(f"/runs/{alice_run}/receipt").status_code == 404
    assert client.get(f"/runs/{alice_run}/audit").status_code == 404
    assert client.get("/analytics/summary").json()["overview"]["runs"] == 0

    r = client.post("/runs?wait=true", json={"idea": "Bob private idea", "max_tasks": 2}).json()
    assert client.get("/runs").json()[0]["run_id"] == r["run_id"]
