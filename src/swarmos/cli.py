"""Command line: ``swarmos run "<idea>"`` and ``swarmos serve``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from swarmos.config import Settings, load_dotenv
from swarmos.contracts import CollaborationMode, Deliverable, RunRequest
from swarmos.gateway.registry import ProviderRegistry, load_provider_configs
from swarmos.orchestrator import Orchestrator
from swarmos.store import Store
from swarmos.synthesis import render_markdown


def _ask(clar: dict) -> list[str] | None:
    """Print Chief's questions and read one-line answers. Empty answers fall back to the assumptions."""
    print(f"\nChief's reading of your idea:\n  {clar['goal']}\n", file=sys.stderr)
    for a in clar.get("assumptions", []):
        print(f"  assumes: {a}", file=sys.stderr)
    print("\nAnswer what you can; press Enter to skip a question.", file=sys.stderr)
    return [input(f"\n{i}. {q}\n> ") for i, q in enumerate(clar["questions"], 1)]


async def _run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.database_url:
        settings.database_url = args.database_url
    if args.providers_file:
        settings.providers_file = args.providers_file
    store = Store(settings.database_url)
    await store.init()
    registry = ProviderRegistry.from_configs(load_provider_configs(settings.providers_file))
    orch = Orchestrator(settings, registry, store)
    req = RunRequest(idea=args.idea, mode=CollaborationMode(args.mode), cross_review=args.cross_review, clarify=args.clarify, language=args.lang,
                     budget_usd=args.budget, max_tasks=args.max_tasks, concurrency=args.concurrency,
                     providers=args.providers.split(",") if args.providers else None)
    try:
        run_id, _ = await orch.submit(req)
        await orch.wait(run_id)
        row = await store.get_run(run_id)
        if row.status == "awaiting_input":  # Chief has questions before it dispatches anything
            ev = await store.find_event(run_id, "run.clarification.requested")
            answers = _ask(ev.payload) if sys.stdin.isatty() else None
            await orch.answer(run_id, answers or [], skip=answers is None)
            await orch.wait(run_id)
            row = await store.get_run(run_id)
        if row.deliverable is None:
            print(f"run {run_id} produced no result: {row.error or row.status}", file=sys.stderr)
            return 1
        d = Deliverable.model_validate(row.deliverable)
    finally:
        await orch.shutdown()
        await store.close()
    out = json.dumps(d.model_dump(mode="json"), indent=2, ensure_ascii=False) if args.json else render_markdown(d)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(out)
        print(f"wrote {args.out} (run {d.run_id}, status {d.status.value})")
    else:
        print(out)
    return 0 if d.status.value in ("completed", "partial") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="swarmos")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one idea end to end and print the deliverable")
    r.add_argument("idea")
    r.add_argument("--mode", choices=[m.value for m in CollaborationMode], default="dynamic")
    r.add_argument("--cross-review", action="store_true")
    r.add_argument("--lang", choices=["en", "fr", "zh"], help="output language (default: same as the idea)")
    r.add_argument("--clarify", action="store_true", help="Chief asks up to 3 questions before dispatching")
    r.add_argument("--budget", type=float, default=2.0, help="per-run budget in USD")
    r.add_argument("--max-tasks", type=int, default=6)
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--providers", help="comma-separated provider ids to restrict to")
    r.add_argument("--providers-file", help="TOML provider config (default: ./providers.toml, else demo models)")
    r.add_argument("--database-url")
    r.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    r.add_argument("--out", help="write output to this file")

    sub.add_parser("setup", help="connect a model: your API key, an OpenAI-compatible endpoint, or a subscription CLI")
    sub.add_parser("providers", help="list configured models and whether their keys are present")

    s = sub.add_parser("serve", help="start the web page and API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    args = p.parse_args(argv)
    load_dotenv()  # ./.env, never overrides variables already exported
    if args.cmd == "run":
        return asyncio.run(_run(args))
    if args.cmd == "setup":
        import getpass

        from swarmos.setup import run_setup
        if not sys.stdin.isatty():
            print("`swarmos setup` asks questions; run it in a terminal.", file=sys.stderr)
            return 1
        return run_setup(input, getpass.getpass, print)
    if args.cmd == "providers":
        from swarmos.setup import describe
        print("\n".join(describe()))
        return 0
    import uvicorn

    uvicorn.run("swarmos.api:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
