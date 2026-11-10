"""Blind A/B test: is the MuseOS result more useful than one single model's answer?

Both sides are shown in the same shape (``BriefAnswer``) with provider names removed,
so the owner cannot tell which is which until after voting.
"""

from __future__ import annotations

import re
from typing import Any

from museos.contracts import BriefAnswer

TARGET_VOTES = 10
PASS_WINS = 6
_REVIEWER_LINE = re.compile(r"^\[\S+ → \S+\]\s*")  # per-agent reviewer notes, see synthesis.synthesize


def _scrub(text: str, names: list[str]) -> str:
    text = _REVIEWER_LINE.sub("", text)
    for n in sorted((n for n in names if n), key=len, reverse=True):
        text = re.sub(re.escape(n), "a model", text, flags=re.IGNORECASE)
    return text


def museos_brief(deliverable: dict[str, Any], provider_names: list[str]) -> dict[str, Any]:
    """Project a MuseOS deliverable onto the shared shape, without anything that reveals its origin."""
    def clean(items: list[str]) -> list[str]:
        return [_scrub(str(x), provider_names) for x in items]

    # Reviewer notes are per-agent detail; the synthesizer's own disagreements come first in the list.
    own = [x for x in deliverable.get("disagreements", []) if not _REVIEWER_LINE.match(str(x))]
    return BriefAnswer(
        executive_summary=_scrub(deliverable.get("executive_summary", ""), provider_names),
        key_points=clean(deliverable.get("agreements", [])),
        open_questions=clean(own),
        next_steps=clean(deliverable.get("next_steps", [])),
        risks=clean(deliverable.get("risks", [])),
        assumptions=clean(deliverable.get("assumptions", [])),
    ).model_dump()


def baseline_brief(baseline: dict[str, Any], provider_names: list[str]) -> dict[str, Any]:
    b = BriefAnswer.model_validate(baseline)
    return BriefAnswer(
        executive_summary=_scrub(b.executive_summary, provider_names),
        **{k: [_scrub(x, provider_names) for x in getattr(b, k)]
           for k in ("key_points", "open_questions", "next_steps", "risks", "assumptions")},
    ).model_dump()


def comparison_state(run_status: str, has_deliverable: bool, baseline_status: str, choice: str | None) -> str:
    if choice:
        return "voted"
    if run_status == "awaiting_input":
        return "awaiting_input"
    if run_status in ("failed", "cancelled") or baseline_status == "failed":
        return "failed"
    if run_status in ("completed", "partial") and has_deliverable and baseline_status == "completed":
        return "ready"
    return "running"


def tally(rows: list[Any]) -> dict[str, Any]:
    museos = baseline = ties = 0
    for r in rows:
        if not r.choice:
            continue
        if r.choice == "tie":
            ties += 1
        elif r.choice == r.museos_slot:
            museos += 1
        else:
            baseline += 1
    voted = museos + baseline + ties
    verdict = "in_progress"
    if voted >= TARGET_VOTES:
        verdict = "keep_multi_agent" if museos >= PASS_WINS else "simplify"
    return {"voted": voted, "target": TARGET_VOTES, "museos_wins": museos, "baseline_wins": baseline,
            "ties": ties, "pass_wins": PASS_WINS, "verdict": verdict}
