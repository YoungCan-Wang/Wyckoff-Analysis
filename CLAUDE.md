# Claude Code Project Instructions

Read and follow all rules in [AGENTS.md](./AGENTS.md) — that is the canonical quality spec.

## Additional Claude-specific guidance

- When modifying existing functions, check if the result exceeds 50 lines. If so, split before committing.
- After any code change, run `python scripts/quality_gate.py --check-functions` to verify.
- Prefer editing existing files over creating new ones.
- When adding features to the web app, implement them as Agent tools in `web/apps/web/src/lib/chat-agent.ts`, not as new routes.
- The legacy function whitelist (`.metrics/func_whitelist.json`) exists for historical debt. When you touch a whitelisted function, try to bring it under the 50-line limit — but don't refactor unrelated code.
