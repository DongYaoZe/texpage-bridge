---
name: texpage-bridge
description: Use this skill when an agent needs to compile an allow-listed local Git worktree through texpage-bridge without accessing TeXPage credentials, browser state, or administrative controls.
---

# TeXPage bridge

Use the bundled low-privilege adapter for TeXPage compilation. Treat the bridge checkout, broker, and local project registry as infrastructure owned by the user or operator.

## Safety boundary

- Use only `build`, `submit`, `request`, `requests`, and `status` through `scripts/texpage_agent.py`.
- Do not invoke `publish`, `versions`, `reserve-version`, `set-version`, or `broker` from this skill.
- Do not read or print `projects.json`, `projects.local.json`, `requests/`, broker logs, Chromium profiles, cookies, tokens, signed artifact URLs, Git credential material, or private TeXPage endpoints.
- Use only a project alias already supplied by the user, repository instructions, or an authorized task. Do not enumerate aliases from the private registry.
- The adapter does not contain TeXPage credentials. It delegates an allow-listed request to the existing bridge checkout, which keeps privileged browser/Git state behind the broker.

## Setup

Set `TEXPAGE_BRIDGE_HOME` to the texpage-bridge checkout. The checkout must already have its local `projects.json` and broker environment configured by the operator.

Windows PowerShell example:

```powershell
$env:TEXPAGE_BRIDGE_HOME = 'D:\path\to\texpage-bridge'
```

POSIX shell example:

```sh
export TEXPAGE_BRIDGE_HOME=/path/to/texpage-bridge
```

The adapter itself is cross-platform Python 3.11+ and does not require a DeepSeek API key.

## Build workflow

For a normal compile where the agent should wait for the result:

```sh
python scripts/texpage_agent.py PROJECT build
```

For a long compile that should be submitted first:

```sh
python scripts/texpage_agent.py PROJECT submit
python scripts/texpage_agent.py PROJECT request REQUEST_ID
```

To inspect recent request state:

```sh
python scripts/texpage_agent.py PROJECT requests --limit 20
```

To read the last successful local build record without compiling:

```sh
python scripts/texpage_agent.py PROJECT status
```

Use `--no-push` only when the task explicitly wants the already-selected remote TeXPage version compiled without freezing and pushing the current worktree.

## Result handling

A successful build writes sanitized local artifacts into the target repository's ignored `.texpage/` directory, normally `latest.pdf`, `latest.log`, and `build.json`. Report those local artifact paths and useful QA metrics. Do not try to recover or expose the broker's signed download URLs.

If the bridge reports login/session expiry or another privileged infrastructure problem, stop at that error and ask the operator to repair the bridge. Do not attempt to inspect browser storage, credentials, or private endpoints.

## Compatibility note

This is an Agent Skill plus a thin CLI adapter, not an official DeepSeek plugin ABI. See the repository `COMPATIBILITY.md` for the currently verified CodeWhale / legacy DeepSeek-TUI and third-party harness landscape.
