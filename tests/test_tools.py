"""Read-only web tools: guards, extraction, policy-gated enrichment through the orchestrator."""

from __future__ import annotations

import asyncio

import pytest

from swarmos import tools
from swarmos.contracts import RunRequest
from tests.conftest import mock_cfg


# ---------------------------------------------------------------- pure units
def test_host_guard_blocks_private_addresses():
    for host in ("127.0.0.1", "localhost", "10.0.0.1", "192.168.1.1", "169.254.1.1", "0.0.0.0", "::1"):
        assert not asyncio.run(tools._host_allowed(host)), host
    assert asyncio.run(tools._host_allowed("example.com"))


def test_html_to_text_strips_script_and_style():
    html = ("<html><head><script>evil()</script><style>x{}</style></head>"
            "<body><h1>Hi</h1><p>Hello <b>world</b>.</p></body></html>")
    text = tools.html_to_text(html)
    assert "Hi" in text and "Hello world." in text
    assert "evil()" not in text and "x{}" not in text


def test_ddg_html_parser_extracts_title_url_snippet():
    html = ('<a class="result__a" href="https://a.com">Title A</a>'
            '<a class="result__snippet">Snip A</a>')
    assert tools._parse_ddg_html(html) == [{"title": "Title A", "url": "https://a.com", "snippet": "Snip A"}]


def test_web_fetch_refuses_non_http_scheme():
    with pytest.raises(tools.ToolError):
        asyncio.run(tools.web_fetch("file:///etc/passwd"))


# ---------------------------------------------------------------- orchestration
async def test_research_enrichment_runs_and_is_audited(store, make_orch, monkeypatch):
    calls = {"search": 0, "fetch": 0}

    async def fake_search(query, max_results=5):
        calls["search"] += 1
        return [{"title": "Fake hit", "url": "https://example.com/x", "snippet": "snippet text"}]

    async def fake_fetch(url, max_bytes=20000):
        calls["fetch"] += 1
        return "Fake page body with facts."

    monkeypatch.setattr(tools, "web_search", fake_search)
    monkeypatch.setattr(tools, "web_fetch", fake_fetch)

    orch = make_orch([mock_cfg("alpha")])
    req = RunRequest(idea="Research the tea market", max_tasks=4, research=True, cross_review=False)
    run_id, _ = await orch.submit(req)
    await orch.wait(run_id)
    row = await store.get_run(run_id)
    assert row.status == "completed" and row.deliverable is not None

    tool_rows = await store.list_tool_runs(run_id)
    used = [t.tool for t in tool_rows]
    assert "web_search" in used and calls["search"] >= 1
    assert calls["fetch"] >= 1 and all(t.status == "completed" for t in tool_rows)
    decisions = await store.list_policy_decisions(run_id)
    assert any(d.action == "tool.web_search" and d.decision == "ALLOW" for d in decisions)


async def test_research_off_means_no_tool_calls(store, make_orch, monkeypatch):
    async def fake_search(query, max_results=5):
        raise AssertionError("search must not run when research=False")

    monkeypatch.setattr(tools, "web_search", fake_search)
    orch = make_orch([mock_cfg("alpha")])
    req = RunRequest(idea="Research the tea market", max_tasks=4, research=False, cross_review=False)
    run_id, _ = await orch.submit(req)
    await orch.wait(run_id)
    assert await store.list_tool_runs(run_id) == []


async def test_tools_blocked_when_server_switch_off(store, make_orch, monkeypatch):
    async def fake_search(query, max_results=5):
        raise AssertionError("search must not run when tools_enabled=False")

    monkeypatch.setattr(tools, "web_search", fake_search)
    orch = make_orch([mock_cfg("alpha")])
    orch.settings.tools_enabled = False
    req = RunRequest(idea="Research the tea market", max_tasks=4, research=True, cross_review=False)
    run_id, _ = await orch.submit(req)
    await orch.wait(run_id)
    assert await store.list_tool_runs(run_id) == []
