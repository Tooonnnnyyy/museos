# Contributing

Thanks for helping. A few rules keep the project healthy.

## Before you start

- Open an issue first for anything larger than a small fix, so we can agree on the approach.
- The core goal is convergence: one idea in, one merged answer out. Features that add agents, channels or
  integrations without serving that goal will usually be declined.

## Development

```bash
uv sync
uv run pytest -q
```

- Every model call goes through `engine.call_model`. Never call an adapter directly.
- Tests use mock providers and a temporary SQLite file. Do not add tests that call a real model.
- Never commit a key, a `.env`, a `providers.toml` or run output.
- Every user-facing string exists in English, French and Chinese: page strings in `I18N` in
  `src/swarmos/web/index.html`, server notes in `src/swarmos/i18n.py`. A test fails if a key is missing.
- Do not ship model IDs or prices you have not checked in the provider's own documentation.

## Licence of contributions

SwarmAgentOS is licensed under the GNU AGPL v3 or later (see `LICENSE`). The maintainers also intend to offer
SwarmAgentOS under separate commercial terms. By submitting a contribution you agree that:

1. your contribution is licensed under the AGPL v3 or later, and
2. you grant the SwarmAgentOS maintainers a perpetual, worldwide, non-exclusive, royalty-free right to license
   your contribution under other terms as well, including commercial ones.

If you cannot agree to this, please open an issue instead of a pull request.
