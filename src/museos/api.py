"""FastAPI surface for the Core Engine."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from museos import blindtest
from museos.config import Settings
from museos.contracts import RunRequest
from museos.gateway.registry import ProviderRegistry, load_provider_configs
from museos.orchestrator import Orchestrator
from museos.store import Store


class Answers(BaseModel):
    answers: list[str] = Field(default_factory=list, max_length=3)
    skip: bool = False


class Vote(BaseModel):
    choice: str = Field(pattern="^(A|B|tie)$")
    note: str | None = Field(default=None, max_length=2000)


def create_app(settings: Settings | None = None, registry: ProviderRegistry | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.database_url)
        await store.init()
        reg = registry or ProviderRegistry.from_configs(load_provider_configs(settings.providers_file))
        app.state.store = store
        app.state.registry = reg
        app.state.orch = Orchestrator(settings, reg, store)
        yield
        await app.state.orch.shutdown()
        await store.close()

    app = FastAPI(title="MuseOS Core Engine", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def same_origin_writes(request, call_next):
        """Refuse state-changing requests sent by another website (CSRF). Browsers attach an Origin header
        to cross-site POSTs; it must name one of the allowed hosts. Non-browser clients send none."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin is not None:
                from urllib.parse import urlsplit

                host = urlsplit(origin).hostname or ""
                allowed = {h.strip("[]") for h in settings.allowed_hosts}
                if host not in allowed:
                    return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def command_center() -> str:
        return (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def admin_page() -> str:
        return (Path(__file__).parent / "web" / "admin.html").read_text(encoding="utf-8")

    @app.get("/admin/summary")
    async def admin_summary() -> dict[str, Any]:
        from museos.admin import summarize

        return await summarize(app.state.store, app.state.registry, settings)

    @app.post("/admin/providers/{provider_id}/reset")
    async def admin_reset_provider(provider_id: str) -> dict[str, Any]:
        """Close a provider's circuit breaker so it is tried again at once."""
        if provider_id not in app.state.registry.configs:
            raise HTTPException(404, "provider not found")
        app.state.registry.record_success(provider_id)
        return {"provider": provider_id, "healthy": app.state.registry.is_healthy(provider_id)}

    @app.get("/runs")
    async def list_runs(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
        cmps = {c.run_id: c for c in await app.state.store.list_comparisons(500)}
        return [{"run_id": r.id, "status": r.status, "idea": r.idea[:200], "spent_usd": r.spent_usd,
                 "created_at": r.created_at.isoformat(),
                 "comparison_id": cmps[r.id].id if r.id in cmps else None,
                 "blind": r.id in cmps and not cmps[r.id].choice}
                for r in await app.state.store.list_runs(limit)]

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "providers_enabled": len(app.state.registry.enabled_ids())}

    @app.get("/providers")
    async def providers() -> list[dict[str, Any]]:
        return app.state.registry.status()

    @app.post("/runs", status_code=202)
    async def create_run(req: RunRequest, response: Response, wait: bool = Query(False),
                         idempotency_key: str | None = Header(None, alias="Idempotency-Key")) -> dict[str, Any]:
        orch: Orchestrator = app.state.orch
        if req.budget_usd > settings.max_budget_usd:
            raise HTTPException(422, f"budget_usd exceeds the server limit of {settings.max_budget_usd}")
        run_id, created = await orch.submit(req, idempotency_key=idempotency_key)
        if not created:
            response.status_code = 200
        if wait:
            await orch.wait(run_id)
            response.status_code = 200
        return await _run_view(run_id, include_deliverable=wait) | {"created": created}

    @app.post("/runs/{run_id}/answers")
    async def answer_run(run_id: str, body: Answers) -> dict[str, Any]:
        await _require(run_id)
        if not await app.state.orch.answer(run_id, body.answers, skip=body.skip):
            raise HTTPException(409, "run is not waiting for answers")
        return await _run_view(run_id, include_deliverable=False)

    # ------------------------------------------------------------ blind test
    def _provider_names() -> list[str]:
        out = []
        for cfg in app.state.registry.configs.values():
            out += [cfg.id, cfg.display_name, cfg.model, cfg.model.split("/")[-1]]
        return out

    async def _comparison_view(c) -> dict[str, Any]:
        run = await _require(c.run_id)
        state = blindtest.comparison_state(run.status, run.deliverable is not None, c.baseline_status, c.choice)
        view: dict[str, Any] = {"comparison_id": c.id, "run_id": c.run_id, "idea": run.idea, "state": state,
                                "created_at": c.created_at.isoformat()}
        if state in ("ready", "voted"):
            names = _provider_names()
            sides = {c.museos_slot: blindtest.museos_brief(run.deliverable, names),
                     ("B" if c.museos_slot == "A" else "A"): blindtest.baseline_brief(c.baseline, names)}
            view["A"], view["B"] = sides["A"], sides["B"]
        if state == "voted":  # identities are only revealed after the vote
            view["reveal"] = {
                "choice": c.choice, "note": c.note, "museos_slot": c.museos_slot,
                "museos": {"providers": (run.deliverable or {}).get("providers_used", []),
                           "cost_usd": run.spent_usd},
                "baseline": {"provider": c.baseline_provider, "model": c.baseline_model,
                             "cost_usd": c.baseline_cost_usd, "latency_ms": c.baseline_latency_ms},
            }
        if state == "failed":
            view["error"] = "One side did not finish, so this test does not count."
        return view

    @app.get("/comparisons")
    async def list_comparisons() -> dict[str, Any]:
        rows = await app.state.store.list_comparisons(200)
        return {"tally": blindtest.tally(rows), "items": [await _comparison_view(c) for c in rows[:30]]}

    @app.get("/comparisons/{comparison_id}")
    async def get_comparison(comparison_id: str) -> dict[str, Any]:
        c = await app.state.store.get_comparison(comparison_id)
        if c is None:
            raise HTTPException(404, "comparison not found")
        return await _comparison_view(c)

    @app.post("/comparisons/{comparison_id}/vote")
    async def vote(comparison_id: str, body: Vote) -> dict[str, Any]:
        from datetime import UTC, datetime

        c = await app.state.store.get_comparison(comparison_id)
        if c is None:
            raise HTTPException(404, "comparison not found")
        view = await _comparison_view(c)
        if view["state"] != "ready":
            raise HTTPException(409, f"comparison is {view['state']}, not ready for a vote")
        await app.state.store.update_comparison(comparison_id, choice=body.choice, note=body.note,
                                                voted_at=datetime.now(UTC))
        return await _comparison_view(await app.state.store.get_comparison(comparison_id))

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return await _run_view(run_id, include_deliverable=True)

    @app.get("/runs/{run_id}/events")
    async def get_events(run_id: str) -> list[dict[str, Any]]:
        await _require(run_id)
        return [{"seq": e.seq, "event_id": e.event_id, "event_type": e.event_type, "task_id": e.task_id,
                 "occurred_at": e.occurred_at.isoformat(), "payload": e.payload}
                for e in await app.state.store.list_events(run_id)]

    @app.get("/runs/{run_id}/audit")
    async def get_audit(run_id: str) -> dict[str, Any]:
        await _require(run_id)
        s: Store = app.state.store
        return {
            "agent_runs": [{"task": a.task_key, "attempt": a.attempt, "actor": a.actor, "provider": a.provider,
                            "model": a.model, "status": a.status, "error": a.error, "latency_ms": a.latency_ms}
                           for a in await s.list_agent_runs(run_id)],
            "policy_decisions": [{"task": p.task_key, "actor": p.actor, "action": p.action,
                                  "resource": p.resource, "decision": p.decision, "rule": p.rule,
                                  "reason": p.reason} for p in await s.list_policy_decisions(run_id)],
            "usage": [{"task": u.task_key, "provider": u.provider, "model": u.model,
                       "input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "cost_usd": u.cost_usd,
                       "cost_is_estimate": bool(u.cost_is_estimate)} for u in await s.list_usage(run_id)],
        }

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        await _require(run_id)
        requested = app.state.orch.cancel(run_id) or await app.state.orch.cancel_waiting(run_id)
        return {"run_id": run_id, "cancel_requested": requested}

    async def _require(run_id: str):
        row = await app.state.store.get_run(run_id)
        if row is None:
            raise HTTPException(404, "run not found")
        return row

    async def _run_view(run_id: str, include_deliverable: bool) -> dict[str, Any]:
        row = await _require(run_id)
        tasks = await app.state.store.list_tasks(run_id)
        clar = await app.state.store.find_event(run_id, "run.clarification.requested")
        cmp = await app.state.store.get_comparison_by_run(run_id)
        stats: dict[str, dict[str, Any]] = {}
        for u in await app.state.store.list_usage(run_id):
            s = stats.setdefault(u.task_key, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                                              "attempts": 0, "latency_ms": 0})
            s["input_tokens"] += u.input_tokens
            s["output_tokens"] += u.output_tokens
            s["cost_usd"] += u.cost_usd
        for a in await app.state.store.list_agent_runs(run_id):
            s = stats.setdefault(a.task_key, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                                              "attempts": 0, "latency_ms": 0})
            s["attempts"] += 1
            s["latency_ms"] += a.latency_ms
        empty = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "attempts": 0, "latency_ms": 0}
        view = {
            "run_id": row.id, "status": row.status, "idea": row.idea, "spent_usd": row.spent_usd,
            "error": row.error, "plan": row.plan,
            "tasks": [{"task_id": t.task_key, "kind": t.kind, "role": t.role, "status": t.status,
                       "provider": t.provider, "model": t.model, "error": t.error,
                       "title": (t.spec or {}).get("title"), "depends_on": (t.spec or {}).get("depends_on", []),
                       **stats.get(t.task_key, empty)} for t in tasks],
            "chief": {k: stats[k] for k in ("__plan__", "__synthesis__") if k in stats},
            "created_at": row.created_at.isoformat(),
            "clarification": clar.payload if clar else None,
            "comparison_id": cmp.id if cmp else None,
            "blind": bool(cmp and not cmp.choice),
        }
        if include_deliverable and not view["blind"]:  # a blind test hides its result until the vote
            view["deliverable"] = row.deliverable
        elif include_deliverable:
            view["deliverable"] = None
        return view

    return app


app = create_app()
