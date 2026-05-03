# Wyckoff-Analysis Development Rules

> This file is the single source of truth for code quality rules.
> All AI coding assistants (Claude Code, Cursor, Copilot, Windsurf, etc.) MUST follow these rules.

## Project Overview

A-share quantitative analysis system based on Wyckoff method. Python backend (CLI + Streamlit + MCP) + React/TypeScript web frontend.

## Quick Commands

```bash
# Python
python -m pytest tests/ -x -q           # run tests
ruff check .                             # lint
ruff format --check .                    # format check
python scripts/quality_gate.py --ci      # function length + LOC trend

# Web (from web/ directory)
pnpm dev                                 # dev server
pnpm build                               # production build
pnpm -r exec tsc --noEmit                # typecheck
```

## Hard Rules (CI enforced, will block merge)

1. **Function length ≤ 50 lines** — No single function/method may exceed 50 lines of code. If it does, split it. Legacy violations are whitelisted in `.metrics/func_whitelist.json`; new violations or worsening existing ones will fail CI.

2. **Pass ruff check** — All Python code must pass `ruff check .` with the project config in `pyproject.toml`.

3. **Pass ruff format** — All Python code must be formatted with `ruff format`.

4. **Pass TypeScript strict mode** — Web code must compile with `tsc --noEmit` (strict: true, noUnusedLocals, noUnusedParameters).

5. **Pass pytest** — All tests must pass. Tests must not make real network calls.

## Soft Rules (quality expectations)

1. **No code bloat** — If 50 lines can do the job, don't write 80. Code volume is tracked in `.metrics/loc.json`; growth >5% without corresponding feature additions will be flagged.

2. **No dead code** — Don't leave unused imports, commented-out blocks, or unreachable branches. Delete them.

3. **Comments: only when WHY is non-obvious** — Don't explain what code does. Don't reference tickets or tasks. Only explain hidden constraints or surprising behavior.

4. **No debug artifacts** — Don't commit console.log, print(), breakpoint(), or TODO/FIXME comments.

## Architecture Constraints

- **Web: no new pages** — New features go into the Agent (chat) interface, not as separate routes.
- **Data isolation: Route A** — Signals are shared; portfolio and settings are per-user.
- **Python ≥ 3.11**, **Node ≥ 20**, **pnpm** for web workspace.

## Commit Messages

Use conventional prefixes: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`.

## Before Submitting Code

```bash
ruff check . && ruff format --check . && python scripts/quality_gate.py --check-functions
```
