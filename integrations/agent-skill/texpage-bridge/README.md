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

## Why keep the Skill + CLI fallback

The official `deepseek-ai/deepseek-harness` now has a real Cordis/dsh plugin integration in `../../deepseek-harness/texpage-bridge/`. This Skill remains useful for CodeWhale and other `SKILL.md`-aware runtimes that do not load the official Harness bundle format.

The official dsh plugin ABI should not be generalized to every third-party project that happens to use a DeepSeek or `deepseek-harness` name. This fallback keeps a small subprocess contract and does not couple TeXPage credentials or broker internals to any one agent runtime. See `../../../COMPATIBILITY.md` for the pinned official Harness snapshot and the separate generic compatibility claims.
