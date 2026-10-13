# Agent skills

## Issue tracker

Use GitHub Issues through `gh`. Read `docs/agents/issue-tracker.md` before ticket work.

## Domain docs

This repository uses one domain context. Before exploration or design, read existing context and relevant ADRs as described in `docs/agents/domain.md`.

## Validation

Keep the production script standalone and standard-library only. Run `python -m unittest discover` after behavior changes. Use a scratch `CLAUDE_CONFIG_DIR` and `STATUSLINE_LIVE_USAGE=0` for manual renders; mock font installation and terminal settings in tests.
