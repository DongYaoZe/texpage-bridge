# Agent and DeepSeek ecosystem compatibility

Last verified: 2026-08-20.

texpage-bridge does **not** claim an official DeepSeek plugin ABI. The integration shipped here is deliberately a portable Agent Skill (`SKILL.md`) plus a narrow local CLI adapter. It does not call the DeepSeek API and does not require a DeepSeek API key.

## What was verified

Primary/upstream sources checked for this compatibility note:

- DeepSeek organization agent integration index: https://github.com/deepseek-ai/awesome-deepseek-agent
- DeepSeek organization's DeepSeek-TUI integration guide: https://github.com/deepseek-ai/awesome-deepseek-agent/blob/main/docs/deepseek-tui.md
- CodeWhale upstream (renamed from DeepSeek-TUI): https://github.com/Hmbown/CodeWhale
- CodeWhale rebrand/migration note: https://github.com/Hmbown/CodeWhale/blob/main/docs/REBRAND.md
- CodeWhale skill/runtime documentation: https://github.com/Hmbown/CodeWhale/blob/main/README.md
- CodeWhale releases: https://github.com/Hmbown/CodeWhale/releases
- HenryZ838978/deepseek-harness: https://github.com/HenryZ838978/deepseek-harness
- tylerbuilds/deepseek-harness: https://github.com/tylerbuilds/deepseek-harness

The DeepSeek organization guide still documents the project under its former DeepSeek-TUI name. The upstream project has since renamed to CodeWhale; its migration document states that `~/.codewhale/` is now the preferred state root while legacy `~/.deepseek/` state remains a compatibility fallback. CodeWhale continues to discover `SKILL.md` packages from common skill directories. The published release explicitly verified here is `v0.9.1`; newer source candidates and milestones are not treated as shipped compatibility targets until their release artifacts are verified.

The name `deepseek-harness` is not a unique standard. For example, `HenryZ838978/deepseek-harness` and `tylerbuilds/deepseek-harness` are separate third-party projects with different purposes and interfaces. The former publishes a Python package, CLI, MCP server, and Skill package; the latter exposes its own CLI/MCP runtime. Neither name is treated here as a DeepSeek-wide plugin ABI.

## Compatibility matrix

| Runtime / project | Verified surface | texpage-bridge status | Claim boundary |
| --- | --- | --- | --- |
| `Hmbown/CodeWhale` `v0.9.1` (formerly DeepSeek-TUI) | `SKILL.md` discovery remains documented; the v0.9.x product uses `codewhale` and prefers `~/.codewhale/skills` | **Format-compatible** with `integrations/agent-skill/texpage-bridge`; local adapter is tested independently | No claim of DeepSeek ownership or an official universal plugin ABI; no end-to-end CodeWhale test is performed by this repository |
| `deepseek-ai/awesome-deepseek-agent` | DeepSeek-organization integration index and DeepSeek-TUI guide | **Reference source only** | This repository is documentation/indexing, not a plugin host ABI |
| `HenryZ838978/deepseek-harness` `0.2.0` | Third-party Python/CLI/MCP/Skill packaging | **No direct harness binding**; the TeXPage Skill follows a compatible broad `SKILL.md` convention | Do not infer interoperability with its Python/MCP protocol wrapper from the shared word "harness" |
| `tylerbuilds/deepseek-harness` | Third-party CLI and MCP server | **No direct harness binding** | Its MCP server is a separate tool surface; texpage-bridge does not depend on it |
| Other `SKILL.md`-aware agents | Common Markdown skill convention plus local shell execution | **Portable by design** when the host can run Python 3.11+ and preserve the configured bridge checkout | Host-specific discovery paths, approval rules, and sandbox behavior must be checked separately |

## Why this repository ships a Skill + CLI

The useful stable contract is smaller than any model-specific plugin system:

1. an agent receives an allow-listed local project alias;
2. it can request `build` or `submit`;
3. it can inspect `request`, `requests`, or `status`;
4. privileged TeXPage identifiers, browser state, credentials, signed URLs, version administration, and broker controls stay behind texpage-bridge.

That contract maps naturally to a `SKILL.md` plus a subprocess CLI. An MCP server can be added later if there is a concrete host requirement, but it is not necessary to expose this workflow and would add another long-lived process and configuration surface.

## NJU-specific backend assumptions

The **agent adapter is provider-neutral**, but the current bridge/broker implementation is not. The backend currently assumes NJU's TeXPage deployment in several places, including:

- editor and API base URLs under `https://tex.nju.edu.cn`;
- the Git credential host `git.tex.nju.edu.cn`;
- NJU project/version route shapes and API response fields;
- current compile-control DOM behavior;
- a persistent headed Chromium session because NJU's security layer may reject true headless operation;
- the present Git Credential Manager repair flow.

Another TeXPage-compatible deployment should keep the same low-privilege agent request contract while replacing provider-specific behavior behind the broker. A clean adaptation would make the web/API base URLs, Git host/auth strategy, project/version routes, response-field mapping, compile-control detection, and login/session validation provider configuration or a provider module. Do not move credentials or provider tokens into the Skill package to make another deployment work.

## Security boundary

The integration package must remain safe to publish. In particular it must not include or enumerate:

- `projects.json` / `projects.local.json`;
- `requests/` contents;
- broker logs or PID/runtime state;
- Chromium profiles or browser storage;
- TeXPage/Git credentials or tokens;
- signed PDF/log artifact URLs;
- private deployment endpoints learned from authenticated browser state.

The adapter intentionally exposes no `publish`, version-management, or broker-management command.
