# WORKFLOW — running multiple coding agents in parallel

> How to run several agent CLIs (Claude Code, Codex, pi/omp, …) side by side with kitty + tmux,
> without them stepping on each other. This file is a task book: humans read it to supervise,
> agents read it to know their lane. Point every agent at it in your first message.

## The one rule that makes parallel agents safe

**One agent = one git worktree + one branch. Never two agents in the same directory.**

Everything else in this file is an elaboration of that rule.

## Terminal layout (kitty + tmux)

- **kitty tabs** = projects (`sesa`, `swarmos`, `personal`). One tab per repo.
- **tmux sessions** = workstreams inside a project (`sesa-frontend`, `sesa-review`).
  tmux survives a closed terminal; kitty tabs do not. If you SSH in, use tmux.
- **Panes per session**:
  | Pane | Content |
  |---|---|
  | 1 | The working agent (CLI) |
  | 2 | `gh` console — issues, PRs, diffs, checks |
  | 3 | Status panel — `git log --oneline -5; git status -s; gh pr list` on a loop or on demand |
  | 4 | Optional: another agent on a *different* worktree |

## Launching a workstream (scriptable)

```bash
# per workstream
git worktree add ../<repo>-<lane> -b <lane>
cd ../<repo>-<lane>
tmux new -s <repo>-<lane>            # or a new kitty window: kitty @ new-window --cwd $(pwd)
```

Then hand the agent a task book. **Always in writing**, never by chatting:

```markdown
# Task
<one paragraph: goal, boundaries, acceptance criteria>
Repo: <repo>   Branch: <lane>   Worktree: ../<repo>-<lane>
Rules:
- edit ONLY this worktree; never touch another branch or worktree
- never push to main; open a PR with `gh pr create`
- when done, write a 3-line summary to local://status-<lane>.md
```

## Supervision loop (the part you do)

1. **Review diffs, not chat.** When an agent says it's done: `gh pr view <n>`, `gh pr diff <n> | less`.
2. **CI is the gate.** `gh pr checks <n> --watch`. Merge only green.
3. **Cross-review cheaply** — have a *different* agent review the PR, or run:
   ```bash
   uv run swarmos pr-review <owner/repo> <n>          # writes artifacts/pr-…-review.md
   uv run swarmos pr-review <owner/repo> <n> --post   # + posts the review as a PR comment
   ```
4. **Status on one screen.** Each agent writes its 3-line status to a shared file; you scan one
   place instead of five scrollbacks.

## Feedback loop with stakeholders

1. Push the branch; preview: static → Vercel/Cloudflare Pages; dynamic → `ssh -p 443 -R0:localhost:<port> a.pinggy.io`.
2. Send the preview URL; collect opinions as GitHub issues.
3. Agent edits the *same branch*; re-preview; repeat until consensus.
4. Final: PR to the org repo (`uottawaSESA/…`), squashed, after review. Protect `main` with
   required reviews + CI so agents can never push straight to it.

## Cost and hygiene

- Sessions rot. One task book per session; start a fresh session when the context no longer matters.
- Watch token spend: parallel agents multiply it. Use small models for mechanical edits.
- `.env` never leaves your machine; agents must never commit secrets (`.gitignore` covers `.env*`,
  `providers.toml`, `*.db`).
- Every agent you run has write access to your GitHub as *you*: review every PR before merge.

## SwarmAgentOS inside the workflow

SwarmAgentOS is itself the "cross-review lane": one idea in, several models at work, one merged
answer out, with a blind test that proves (or disproves) the multi-model route on *your* questions.

```bash
uv run swarmos pr-review Tooonnnnyyy/museos 42 --research   # research tasks may search the web
uv run swarmos run "Which note-taking tool should our team adopt" --research --cross-review
```
