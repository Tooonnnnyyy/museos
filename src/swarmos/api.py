"""FastAPI surface for the Core Engine."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from swarmos import blindtest
from swarmos.config import Settings
from swarmos.contracts import RunRequest
from swarmos.gateway.registry import ProviderRegistry, load_provider_configs
from swarmos.orchestrator import Orchestrator
from swarmos.security import hash_password, new_token, normalize_email, valid_email, verify_password
from swarmos.store import Store, UserRow


class Answers(BaseModel):
    answers: list[str] = Field(default_factory=list, max_length=3)
    skip: bool = False


class Vote(BaseModel):
    choice: str = Field(pattern="^(A|B|tie)$")
    note: str | None = Field(default=None, max_length=2000)


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=8, max_length=128)


SESSION_COOKIE = "swarmos_session"


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

    app = FastAPI(title="SwarmAgentOS Core Engine", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def same_origin_writes(request, call_next):
        """Refuse state-changing requests sent by any other origin (CSRF), including another port on this
        machine. Browsers attach an Origin header to cross-origin POSTs; it must equal this server's own
        scheme, host and port. Non-browser clients send no Origin."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            own = f"{request.url.scheme}://{request.headers.get('host', '')}"
            fetch_site = request.headers.get("sec-fetch-site")
            if (origin is not None and origin.rstrip("/").lower() != own.lower()) or \
                    (fetch_site is not None and fetch_site not in ("same-origin", "none")):
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    # ------------------------------------------------------------------ auth helpers
    async def _current_user(request: Request) -> UserRow | None:
        """Resolve the signed-in user from the session cookie; None when auth is off or the
        session is missing/expired. Called on every protected route."""
        if not settings.auth_enabled:
            return None
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        session = await app.state.store.get_session(token)
        if session is None:
            return None
        return await app.state.store.get_user(session.user_id)

    async def _require_user(request: Request) -> UserRow | None:
        """401 without a valid session — unless auth is disabled, in which case anonymous (None)
        access keeps the single-user behavior and the workspace stays "default"."""
        if not settings.auth_enabled:
            return None
        user = await _current_user(request)
        if user is None:
            raise HTTPException(401, "authentication required")
        return user

    def _workspace(user: UserRow | None) -> str:
        return f"user:{user.id}" if user else "default"

    def _set_session_cookie(response: Response, token: str) -> None:
        # secure=False for now: served over plain HTTP (locally and behind an HTTP-leg tunnel).
        # Flip to secure=True when HTTPS terminates at this app.
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                            max_age=settings.session_ttl_days * 86400, path="/")

    def _user_view(user: UserRow) -> dict[str, Any]:
        return {"user": {"id": user.id, "email": user.email}}

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page() -> str:
        return (Path(__file__).parent / "web" / "login.html").read_text(encoding="utf-8")

    @app.post("/auth/signup", status_code=201)
    async def signup(body: Credentials, response: Response) -> dict[str, Any]:
        email = normalize_email(body.email)
        if not valid_email(email):
            raise HTTPException(422, "invalid email address")
        user = await app.state.store.create_user(email, hash_password(body.password))
        if user is None:
            raise HTTPException(409, "email already registered")
        token = new_token()
        await app.state.store.create_session(user.id, token, datetime.now(UTC) + timedelta(days=settings.session_ttl_days))
        _set_session_cookie(response, token)
        return _user_view(user)

    @app.post("/auth/login")
    async def login(body: Credentials, response: Response) -> dict[str, Any]:
        email = normalize_email(body.email)
        user = await app.state.store.get_user_by_email(email)
        if user is None or not verify_password(body.password, user.password_hash):
            raise HTTPException(401, "invalid email or password")
        token = new_token()
        await app.state.store.create_session(user.id, token, datetime.now(UTC) + timedelta(days=settings.session_ttl_days))
        _set_session_cookie(response, token)
        return _user_view(user)

    @app.post("/auth/logout")
    async def logout(request: Request, response: Response) -> dict[str, Any]:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            await app.state.store.delete_session(token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/auth/me")
    async def me(request: Request) -> dict[str, Any]:
        user = await _current_user(request)
        if user is None:
            raise HTTPException(401, "not authenticated")
        return _user_view(user)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def command_center(request: Request) -> Response:
        if settings.auth_enabled and await _current_user(request) is None:
            return RedirectResponse("/login")
        return HTMLResponse((Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8"))

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def admin_page(request: Request) -> Response:
        if settings.auth_enabled and await _current_user(request) is None:
            return RedirectResponse("/login")
        return HTMLResponse((Path(__file__).parent / "web" / "admin.html").read_text(encoding="utf-8"))

    @app.get("/analytics", response_class=HTMLResponse, include_in_schema=False)
    async def analytics_page(request: Request) -> Response:
        if settings.auth_enabled and await _current_user(request) is None:
            return RedirectResponse("/login")
        return HTMLResponse((Path(__file__).parent / "web" / "analytics.html").read_text(encoding="utf-8"))

    @app.get("/analytics/summary")
    async def analytics_summary(days: int | None = Query(None),
                                user: UserRow = Depends(_require_user)) -> dict[str, Any]:
        from swarmos.analytics import summary

        return await summary(app.state.store, days, workspace_id=_workspace(user))

    @app.get("/runs/{run_id}/receipt")
    async def run_receipt(run_id: str, user: UserRow = Depends(_require_user)) -> dict[str, Any]:
        return await _receipt_view(run_id, _workspace(user))

    @app.get("/runs/{run_id}/receipt.md", response_class=Response, include_in_schema=False)
    async def run_receipt_markdown(run_id: str, user: UserRow = Depends(_require_user)) -> Response:
        from swarmos.receipt import receipt_markdown

        return Response(receipt_markdown(await _receipt_view(run_id, _workspace(user))), media_type="text/markdown")

    @app.get("/admin/summary")
    async def admin_summary(user: UserRow = Depends(_require_user)) -> dict[str, Any]:
        from swarmos.admin import summarize

        return await summarize(app.state.store, app.state.registry, settings)

    @app.post("/admin/providers/{provider_id}/reset")
    async def admin_reset_provider(provider_id: str, user: UserRow = Depends(_require_user)) -> dict[str, Any]:
        """Close a provider's circuit breaker so it is tried again at once."""
        if provider_id not in app.state.registry.configs:
            raise HTTPException(404, "provider not found")
        app.state.registry.record_success(provider_id)
        return {"provider": provider_id, "healthy": app.state.registry.is_healthy(provider_id)}

    @app.get("/runs")
    async def list_runs(limit: int = Query(20, ge=1, le=100),
                        user: UserRow = Depends(_require_user)) -> list[dict[str, Any]]:
        workspace = _workspace(user)
        runs = await app.state.store.list_runs(limit, workspace_id=workspace)
        cmps = {c.run_id: c for c in await app.state.store.list_comparisons(500)
                if c.run_id in {r.id for r in runs}}
        return [{"run_id": r.id, "status": r.status, "idea": r.idea[:200], "spent_usd": r.spent_usd,
                 "created_at": r.created_at.isoformat(),
                 "comparison_id": cmps[r.id].id if r.id in cmps else None,
                 "blind": r.id in cmps and not cmps[r.id].choice}
                for r in runs]

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "providers_enabled": len(app.state.registry.enabled_ids())}

    @app.get("/providers")
    async def providers(user: UserRow = Depends(_require_user)) -> list[dict[str, Any]]:
        return app.state.registry.status()

    @app.post("/runs", status_code=202)
    async def create_run(req: RunRequest, response: Response, wait: bool = Query(False),
                         idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                         user: UserRow = Depends(_require_user)) -> dict[str, Any]:
        orch: Orchestrator = app.state.orch
        if req.budget_usd > settings.max_budget_usd:
            raise HTTPException(422, f"budget_usd exceeds the server limit of {settings.max_budget_usd}")
        workspace = _workspace(user)
        req.workspace_id = workspace
        req.project_id = "default"
        run_id, created = await orch.submit(req, idempotency_key=idempotency_key)
        if not created:
            response.status_code = 200
        if wait:
            await orch.wait(run_id)
            response.status_code = 200
        return await _run_view(run_id, include_deliverable=wait, workspace=workspace) | {"created": created}

    @app.post("/runs/{run_id}/answers")
    async def answer_run(run_id: str, body: Answers,
                         user: UserRow = Depends(_require_user)) -> dict[str, Any]:
        workspace = _workspace(user)
        await _require(run_id, workspace)
        if not await app.state.orch.answer(run_id, body.answers, skip=body.skip):
            raise HTTPException(409, "run is not waiting for answers")
        return await _run_view(run_id, include_deliverable=False, workspace=workspace)

    # ------------------------------------------------------------ blind test
    def _provider_names() -> list[str]:
        out = []
        for cfg in app.state.registry.configs.values():
            out += [cfg.id, cfg.display_name, cfg.model, cfg.model.split("/")[-1]]
        return out

    async def _comparison_view(c, workspace: str) -> dict[str, Any]:
        run = await _require(c.run_id, workspace)
        state = blindtest.comparison_state(run.status, run.deliverable is not None, c.baseline_status, c.choice)
        view: dict[str, Any] = {"comparison_id": c.id, "run_id": c.run_id, "idea": run.idea, "state": state,
                                "created_at": c.created_at.isoformat()}
        if state in ("ready", "voted"):
            names = _provider_names()
            sides = {c.swarmos_slot: blindtest.swarmos_brief(run.deliverable, names),
                     ("B" if c.swarmos_slot == "A" else "A"): blindtest.baseline_brief(c.baseline, names)}
            view["A"], view["B"] = sides["A"], sides["B"]
        if state == "voted":  # identities are only revealed after the vote
            view["reveal"] = {
                "choice": c.choice, "note": c.note, "swarmos_slot": c.swarmos_slot,
                "swarmos": {"providers": (run.deliverable or {}).get("providers_used", []),
                           "cost_usd": run.spent_usd},
                "baseline": {"provider": c.baseline_provider, "model": c.baseline_model,
                             "cost_usd": c.baseline_cost_usd, "latency_ms": c.baseline_latency_ms},
            }
        if state == "failed":
            view["error"] = "One side did not finish, so this test does not count."
        return view

    @app.get("/comparisons")
    async def list_comparisons(user: UserRow = Depends(_require_user)) -> dict[str, Any]:
        workspace = _workspace(user)
        runs = {r.id for r in await app.state.store.list_runs(500, workspace_id=workspace)}
        rows = [c for c in await app.state.store.list_comparisons(500) if c.run_id in runs]
        return {"tally": blindtest.tally(rows), "items": [await _comparison_view(c, workspace) for c in rows[:30]]}

    @app.get("/comparisons/{comparison_id}")
    async def get_comparison(comparison_id: str,
                             user: UserRow = Depends(_require_user)) -> dict[str, Any]:
        c = await app.state.store.get_comparison(comparison_id)
        if c is None:
            raise HTTPException(404, "comparison not found")
        return await _comparison_view(c, _workspace(user))

    @app.post("/comparisons/{comparison_id}/vote")
    async def vote(comparison_id: str, body: Vote,
                   user: UserRow = Depends(_require_user)) -> dict[str, Any]:
        c = await app.state.store.get_comparison(comparison_id)
        if c is None:
            raise HTTPException(404, "comparison not found")
        workspace = _workspace(user)
        view = await _comparison_view(c, workspace)
        if view["state"] != "ready":
            raise HTTPException(409, f"comparison is {view['state']}, not ready for a vote")
        await app.state.store.update_comparison(comparison_id, choice=body.choice, note=body.note,
                                                voted_at=datetime.now(UTC))
        return await _comparison_view(await app.state.store.get_comparison(comparison_id), workspace)

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, user: UserRow = Depends(_require_user)) -> dict[str, Any]:
        return await _run_view(run_id, include_deliverable=True, workspace=_workspace(user))

    @app.get("/runs/{run_id}/events")
    async def get_events(run_id: str, user: UserRow = Depends(_require_user)) -> list[dict[str, Any]]:
        workspace = _workspace(user)
        await _require(run_id, workspace)
        return [{"seq": e.seq, "event_id": e.event_id, "event_type": e.event_type, "task_id": e.task_id,
                 "occurred_at": e.occurred_at.isoformat(), "payload": e.payload}
                for e in await app.state.store.list_events(run_id)]

    @app.get("/runs/{run_id}/audit")
    async def get_audit(run_id: str, user: UserRow = Depends(_require_user)) -> dict[str, Any]:
        workspace = _workspace(user)
        await _require(run_id, workspace)
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
    async def cancel_run(run_id: str, user: UserRow = Depends(_require_user)) -> dict[str, Any]:
        await _require(run_id, _workspace(user))
        requested = app.state.orch.cancel(run_id) or await app.state.orch.cancel_waiting(run_id)
        return {"run_id": run_id, "cancel_requested": requested}

    async def _require(run_id: str, workspace: str) -> RunRow:
        row = await app.state.store.get_run(run_id)
        if row is None or row.workspace_id != workspace:
            raise HTTPException(404, "run not found")
        return row

    async def _run_view(run_id: str, include_deliverable: bool, workspace: str) -> dict[str, Any]:
        row = await _require(run_id, workspace)
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

    async def _receipt_view(run_id: str, workspace: str) -> dict[str, Any]:
        from swarmos.receipt import build_receipt

        row = await _require(run_id, workspace)
        s: Store = app.state.store
        return build_receipt(
            row, await s.list_tasks(run_id), await s.list_agent_runs(run_id), await s.list_usage(run_id),
            await s.list_policy_decisions(run_id), await s.list_events(run_id),
            await s.get_comparison_by_run(run_id), await s.list_tool_runs(run_id))

    return app


app = create_app()
