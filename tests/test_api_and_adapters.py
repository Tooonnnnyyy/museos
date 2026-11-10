"""API surface, LiteLLM adapter (stubbed, no network), and optional PostgreSQL run."""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

from museos.api import create_app
from museos.config import Settings
from museos.contracts import RunRequest, RunStatus
from museos.gateway.base import CompletionRequest, ProviderConfig, ProviderError
from museos.gateway.litellm_adapter import LiteLLMAdapter, _map_error
from museos.gateway.registry import ProviderRegistry, load_provider_configs
from museos.orchestrator import Orchestrator
from museos.store import Store
from tests.conftest import mock_cfg

SECRET = "sk-test-DO-NOT-LEAK-123"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PROVIDER_KEY", SECRET)
    cfgs = [mock_cfg("alpha"), mock_cfg("beta"),
            ProviderConfig(id="real", adapter="litellm", model="provider/model-id", enabled=False,
                           api_key_env="FAKE_PROVIDER_KEY")]
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}", retry_backoff_s=0,
                        allowed_hosts=["testserver"], max_budget_usd=2.0)
    with TestClient(create_app(settings, ProviderRegistry.from_configs(cfgs))) as c:
        yield c


def test_api_run_lifecycle(client):
    assert client.get("/health").json()["providers_enabled"] == 2
    r = client.post("/runs?wait=true", json={"idea": "Team task tracker for students", "max_tasks": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed" and body["deliverable"]["sections"]
    run_id = body["run_id"]
    assert client.get(f"/runs/{run_id}").json()["status"] == "completed"
    assert client.get(f"/runs/{run_id}/events").json()[0]["event_type"] == "run.created"
    audit = client.get(f"/runs/{run_id}/audit").json()
    assert audit["agent_runs"] and audit["policy_decisions"] and audit["usage"]
    assert client.post(f"/runs/{run_id}/cancel").json()["cancel_requested"] is False  # already finished
    assert client.get("/runs/nope").status_code == 404


def test_command_center_page_and_run_list(client):
    page = client.get("/")
    assert page.status_code == 200 and "MuseOS Command Center" in page.text
    r = client.post("/runs?wait=true", json={"idea": "Dashboard data", "max_tasks": 2}).json()
    runs = client.get("/runs").json()
    assert runs[0]["run_id"] == r["run_id"] and runs[0]["status"] == "completed"
    view = client.get(f"/runs/{r['run_id']}").json()
    t = view["tasks"][0]
    assert t["attempts"] >= 1 and t["output_tokens"] > 0 and t["cost_usd"] > 0 and "title" in t
    assert "__plan__" in view["chief"]


def test_api_rejects_foreign_host_and_oversized_budget(client):
    assert client.get("/health", headers={"host": "evil.example"}).status_code == 400
    assert client.post("/runs", json={"idea": "x" * 5}, headers={"host": "evil.example"}).status_code == 400
    assert client.post("/runs", json={"idea": "Too expensive", "budget_usd": 50}).status_code == 422


def test_api_idempotency_key(client):
    h = {"Idempotency-Key": "telegram-update-42"}
    a = client.post("/runs?wait=true", json={"idea": "Duplicate webhook", "max_tasks": 2}, headers=h)
    b = client.post("/runs", json={"idea": "Duplicate webhook", "max_tasks": 2}, headers=h)
    assert a.json()["run_id"] == b.json()["run_id"]
    assert b.status_code == 200 and b.json()["created"] is False


def test_secrets_never_exposed(client):
    text = client.get("/providers").text
    assert SECRET not in text and "FAKE_PROVIDER_KEY" in text  # reference only
    r = client.post("/runs?wait=true", json={"idea": "Check leaks", "max_tasks": 2}).json()
    dump = str(r) + str(client.get(f"/runs/{r['run_id']}/audit").json())
    assert SECRET not in dump


def test_config_file_rejects_inline_keys(tmp_path):
    p = tmp_path / "providers.toml"
    p.write_text('[[providers]]\nid="x"\nadapter="litellm"\nmodel="m"\napi_key="sk-123"\n')
    with pytest.raises(ValueError, match="api_key_env"):
        load_provider_configs(p)


def test_example_provider_file_loads():
    cfgs = load_provider_configs("providers.example.toml")
    assert {c.id for c in cfgs} >= {"openai", "anthropic", "gemini", "deepseek"}
    assert all(c.api_key_env and not c.enabled for c in cfgs if c.adapter == "litellm")


# ---------------------------------------------------------------- LiteLLM adapter
async def test_litellm_adapter_passes_key_out_of_band(monkeypatch):
    import litellm

    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        msg = types.SimpleNamespace(content='{"summary": "ok"}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                     usage=types.SimpleNamespace(prompt_tokens=100, completion_tokens=50))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setenv("K_ENV", SECRET)
    cfg = ProviderConfig(id="p", adapter="litellm", model="prov/m", api_key_env="K_ENV",
                         cost_per_1k_input=0.01, cost_per_1k_output=0.02)
    resp = await LiteLLMAdapter(cfg).complete(CompletionRequest(
        model="prov/m", messages=[{"role": "user", "content": "hi"}], metadata={"purpose": "task"}))
    assert captured["api_key"] == SECRET
    assert SECRET not in str(captured["messages"]) and "metadata" not in captured
    assert captured["response_format"] == {"type": "json_object"}
    assert resp.cost_usd == pytest.approx(0.002) and resp.input_tokens == 100


async def test_litellm_adapter_missing_credential():
    cfg = ProviderConfig(id="p", adapter="litellm", model="m", api_key_env="DEFINITELY_UNSET_ENV_VAR")
    with pytest.raises(ProviderError) as e:
        await LiteLLMAdapter(cfg).complete(CompletionRequest(model="m", messages=[]))
    assert e.value.retryable is False and e.value.kind == "missing_credential"


def test_litellm_error_mapping():
    RateLimitError = type("RateLimitError", (Exception,), {})
    AuthenticationError = type("AuthenticationError", (Exception,), {})
    assert _map_error(RateLimitError("x")).retryable is True
    err = _map_error(AuthenticationError(f"bad key {SECRET}"))
    assert err.retryable is False and SECRET not in str(err)


# ---------------------------------------------------------------- PostgreSQL
async def test_full_run_on_postgres(pg_url):
    store = Store(pg_url)
    await store.init()
    try:
        orch = Orchestrator(Settings(database_url=pg_url, retry_backoff_s=0),
                            ProviderRegistry.from_configs([mock_cfg("a"), mock_cfg("b")]), store)
        d = await orch.run(RunRequest(idea="Postgres-backed run", max_tasks=3))
        assert d.status is RunStatus.completed
        assert len(await store.list_tasks(d.run_id)) == 3
        assert (await store.get_run(d.run_id)).deliverable["status"] == "completed"
    finally:
        await store.close()


# ---------------------------------------------------------------- clarify before dispatch
def _wait_status(client, run_id, want, tries=200):
    import time
    for _ in range(tries):
        s = client.get(f"/runs/{run_id}").json()["status"]
        if s in want:
            return s
        time.sleep(0.02)
    raise AssertionError(f"run stayed {s}, wanted {want}")


def test_clarify_waits_for_answers_then_runs_on_the_brief(client):
    r = client.post("/runs", json={"idea": "Vague pet thing", "max_tasks": 2, "clarify": True}).json()
    rid = r["run_id"]
    assert _wait_status(client, rid, {"awaiting_input"}) == "awaiting_input"
    view = client.get(f"/runs/{rid}").json()
    assert len(view["clarification"]["questions"]) == 2 and view["tasks"] == []  # nothing dispatched yet
    done = client.post(f"/runs/{rid}/answers", json={"answers": ["Dog owners in big cities", ""]})
    assert done.status_code == 200
    assert client.post(f"/runs/{rid}/answers", json={"answers": []}).status_code == 409  # cannot answer twice
    _wait_status(client, rid, {"completed"})
    goal = client.get(f"/runs/{rid}").json()["deliverable"]["goal"]
    assert "Dog owners in big cities" in goal and "Vague pet thing" in goal
    assert "Working assumptions" in goal  # one question left unanswered
    kinds = [e["event_type"] for e in client.get(f"/runs/{rid}/events").json()]
    assert kinds.index("run.clarification.requested") < kinds.index("run.clarification.answered") \
        < kinds.index("run.plan.completed")


def test_clarify_skip_and_cancel_while_waiting(client):
    a = client.post("/runs", json={"idea": "Skip the questions", "max_tasks": 2, "clarify": True}).json()["run_id"]
    _wait_status(client, a, {"awaiting_input"})
    assert client.post(f"/runs/{a}/answers", json={"skip": True}).status_code == 200
    _wait_status(client, a, {"completed"})
    b = client.post("/runs", json={"idea": "Cancel while waiting", "clarify": True}).json()["run_id"]
    _wait_status(client, b, {"awaiting_input"})
    assert client.post(f"/runs/{b}/cancel").json()["cancel_requested"] is True
    assert client.get(f"/runs/{b}").json()["status"] == "cancelled"


def test_clear_idea_is_not_blocked(tmp_path):
    cfgs = [mock_cfg("alpha", no_questions=True)]
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'c.db'}", retry_backoff_s=0,
                        allowed_hosts=["testserver"])
    with TestClient(create_app(settings, ProviderRegistry.from_configs(cfgs))) as c:
        r = c.post("/runs?wait=true", json={"idea": "Already clear idea", "max_tasks": 2, "clarify": True}).json()
        rid = r["run_id"]
        assert _wait_status(c, rid, {"completed"}) == "completed"
        assert c.get(f"/runs/{rid}").json()["clarification"] is None


# ---------------------------------------------------------------- blind test
def test_blind_test_hides_identity_until_vote(client):
    r = client.post("/runs", json={"idea": "Blind test idea", "max_tasks": 2, "compare": True,
                                   "cross_review": True}).json()
    rid, cid = r["run_id"], r["comparison_id"]
    assert cid and r["blind"] is True
    import time
    for _ in range(300):
        view = client.get(f"/comparisons/{cid}").json()
        if view["state"] == "ready":
            break
        time.sleep(0.02)
    assert view["state"] == "ready" and "reveal" not in view
    assert client.get(f"/runs/{rid}").json()["deliverable"] is None  # the run view does not leak the result
    blob = str(view["A"]) + str(view["B"])
    assert "alpha" not in blob and "beta" not in blob and "→" not in blob  # no provider names
    assert set(view["A"]) == set(view["B"])  # same shape on both sides
    assert client.post(f"/comparisons/{cid}/vote", json={"choice": "C"}).status_code == 422
    voted = client.post(f"/comparisons/{cid}/vote", json={"choice": "A", "note": "clearer"}).json()
    assert voted["state"] == "voted" and voted["reveal"]["museos_slot"] in ("A", "B")
    assert voted["reveal"]["baseline"]["provider"] in ("alpha", "beta")
    assert client.post(f"/comparisons/{cid}/vote", json={"choice": "B"}).status_code == 409  # one vote only
    t = client.get("/comparisons").json()["tally"]
    assert t["voted"] == 1 and t["museos_wins"] + t["baseline_wins"] == 1 and t["verdict"] == "in_progress"
    assert client.get(f"/runs/{rid}").json()["deliverable"] is not None  # visible after the vote
    audit = client.get(f"/runs/{rid}/audit").json()
    assert any(a["task"] == "__baseline__" and a["actor"] == "baseline" for a in audit["agent_runs"])


def test_blind_test_tally_verdicts():
    from types import SimpleNamespace as R

    from museos.blindtest import tally
    wins = [R(choice="A", museos_slot="A")] * 6 + [R(choice="B", museos_slot="A")] * 3 + [R(choice="tie", museos_slot="B")]
    assert tally(wins)["verdict"] == "keep_multi_agent"
    losses = [R(choice="A", museos_slot="B")] * 6 + [R(choice="A", museos_slot="A")] * 4
    assert tally(losses) | {} == tally(losses) and tally(losses)["verdict"] == "simplify"
    assert tally([R(choice=None, museos_slot="A")])["voted"] == 0


async def test_two_answers_at_once_start_the_run_only_once(make_orch, store):
    import asyncio
    orch = make_orch([mock_cfg("alpha")])
    run_id, _ = await orch.submit(RunRequest(idea="Race on answers", max_tasks=2, clarify=True))
    await orch.wait(run_id)
    assert (await store.get_run(run_id)).status == "awaiting_input"
    results = await asyncio.gather(*[orch.answer(run_id, ["a"]) for _ in range(5)])
    assert sorted(results) == [False, False, False, False, True]
    await orch.wait(run_id)
    plans = [e for e in await store.list_events(run_id) if e.event_type == "run.plan.completed"]
    assert len(plans) == 1


# ---------------------------------------------------------------- languages
def test_language_rule_and_notes():
    from museos.i18n import heading, language_rule, note
    from museos.prompts import baseline_messages, clarify_messages, plan_messages, review_messages
    assert "French" in language_rule("fr") and "Simplified Chinese" in language_rule("zh")
    assert "same language as the project goal" in language_rule(None)
    for msgs in (plan_messages("g", 3, "fr"), clarify_messages("g", "fr"), baseline_messages("g", "fr")):
        assert "in French" in msgs[0]["content"]
    assert "Résultat partiel" in note("fr", "partial", n=1, total=2, detail="x=failed")
    assert note(None, "template_plan") == note("en", "template_plan") and heading("zh", "risks") == "风险"
    from museos.i18n import _HEADINGS, _NOTES
    assert all(set(v) == {"en", "fr", "zh"} for v in {**_NOTES, **_HEADINGS}.values())  # no missing translation


def test_run_in_french_localizes_system_notes_and_markdown(client):
    from museos.contracts import Deliverable
    from museos.synthesis import render_markdown
    r = client.post("/runs?wait=true", json={"idea": "Outil pour valider une idée", "max_tasks": 2, "language": "fr"})
    d = r.json()["deliverable"]
    assert d["language"] == "fr" and any("fournisseurs simulés" in x.lower() for x in d["limitations"])
    md = render_markdown(Deliverable.model_validate(d))
    assert "## Résumé" in md and "## Limites" in md and "**Objectif:**" in md
    assert client.post("/runs", json={"idea": "bad language", "language": "de"}).status_code == 422


def test_page_has_the_same_keys_in_all_three_languages():
    import re
    from pathlib import Path
    html = Path("src/museos/web/index.html").read_text(encoding="utf-8")
    blocks = {}
    for lang, nxt in (("en", "\nfr: {"), ("fr", "\nzh: {"), ("zh", "\n}};")):
        start = html.index(f"\n{lang}: {{")
        blocks[lang] = set(re.findall(r'(?:^|[\s,{])([A-Za-z_][A-Za-z0-9_]*): "', html[start:html.index(nxt, start)]))
    assert len(blocks["en"]) > 100 and blocks["en"] == blocks["fr"] == blocks["zh"]
    used = set(re.findall(r'data-i18n(?:-ph)?="([^"]+)"', html)) | set(re.findall(r'\bt\("([A-Za-z_0-9]+)"', html))
    assert used <= blocks["en"]
