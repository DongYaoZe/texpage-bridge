# texpage-bridge Agent Skill package

This directory is a portable Agent Skill wrapper around texpage-bridge's existing broker boundary.

## Install

Copy this directory into a skill location supported by your agent runtime, then set `TEXPAGE_BRIDGE_HOME` to the texpage-bridge checkout. For example, CodeWhale (the project renamed from DeepSeek-TUI) documents `~/.codewhale/skills/<name>/` as the preferred global location, keeps legacy `~/.deepseek/skills/<name>/` as a migration fallback, and also discovers common `.agents/skills` locations.

For a CodeWhale-style global install, copy the whole `texpage-bridge` directory so the final layout contains `~/.codewhale/skills/texpage-bridge/SKILL.md` and `scripts/texpage_agent.py`. A project-local `.agents/skills/texpage-bridge/` copy is another portable option when the host discovers that convention.

The package intentionally contains no TeXPage project registry, credentials, cookies, request history, browser state, or provider secrets.

## Interface

The only exposed commands are:

- `build`
- `submit`
- `request`
- `requests`
- `status`

Administrative/versioning commands remain outside this package. The adapter validates the project alias and arguments, then launches the checkout's `texpage_bridge.py` as a subprocess. It never opens `projects.json` itself.

## Why a Skill + CLI instead of a claimed DeepSeek plugin

As of 2026-08-20 there is no single DeepSeek-wide plugin ABI that all projects named "deepseek-harness" implement. A `SKILL.md` plus a narrow CLI is portable across skill-aware agents and avoids coupling TeXPage credentials or broker internals to a model-specific runtime. See `../../../COMPATIBILITY.md` for verified upstream references and compatibility claims.
