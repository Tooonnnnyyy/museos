"""swarmos pr-review: gh plumbing, mock-provider review run, artifact, --post."""

from __future__ import annotations

import asyncio
import json
import types

from swarmos import cli as cli_mod


def test_pr_review_runs_and_posts(tmp_path, monkeypatch):
    calls: dict[str, str | None] = {}

    def fake_gh(*argv, input_text=None):
        key = " ".join(argv)
        calls[key] = input_text
        if "pr view" in key:
            return json.dumps({"title": "Add navbar", "body": "Adds the nav component"})
        if "pr diff" in key:
            return "diff --git a/src/nav.py b/src/nav.py\n+def navbar():\n+    return '<nav>'\n"
        if "pr comment" in key:
            return ""
        raise AssertionError(f"unexpected gh call: {key}")

    monkeypatch.setattr(cli_mod, "_gh", fake_gh)
    monkeypatch.chdir(tmp_path)  # artifacts/ lands in the temp dir

    args = types.SimpleNamespace(pr_number=7, repo="octo/repo", post=True, research=False, budget=0.5,
                                 max_tasks=4, concurrency=2, mode="dynamic", lang="en", providers=None,
                                 providers_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path / 'pr.db'}")
    rc = asyncio.run(cli_mod._pr_review(args))
    assert rc == 0
    assert any("pr view" in k for k in calls)
    assert any("pr diff" in k for k in calls)

    artifact = tmp_path / "artifacts" / "pr-octo-repo-7-review.md"
    assert artifact.exists()
    md = artifact.read_text()
    assert "Add navbar" in md  # the deliverable renders the PR title

    comment_calls = [k for k in calls if "pr comment" in k]
    assert len(comment_calls) == 1
    assert "--body-file" in comment_calls[0] and "pr-octo-repo-7-review.md" in comment_calls[0]


def test_pr_review_without_post_does_not_comment(tmp_path, monkeypatch):
    def fake_gh(*argv, input_text=None):
        key = " ".join(argv)
        if "pr view" in key:
            return json.dumps({"title": "T", "body": ""})
        if "pr diff" in key:
            return "diff --git a/x b/x\n+1\n"
        raise AssertionError(f"unexpected gh call: {key}")

    monkeypatch.setattr(cli_mod, "_gh", fake_gh)
    monkeypatch.chdir(tmp_path)

    args = types.SimpleNamespace(pr_number=3, repo="octo/repo", post=False, research=False, budget=0.5,
                                 max_tasks=3, concurrency=2, mode="dynamic", lang="en", providers=None,
                                 providers_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path / 'p2.db'}")
    assert asyncio.run(cli_mod._pr_review(args)) == 0
    assert (tmp_path / "artifacts" / "pr-octo-repo-3-review.md").exists()
