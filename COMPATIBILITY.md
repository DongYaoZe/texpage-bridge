# Agent and DeepSeek ecosystem compatibility

Last verified: 2026-08-20.

texpage-bridge directly supports the **official DeepSeek Harness** at `deepseek-ai/deepseek-harness` through the installable Cordis/dsh bundle in `integrations/deepseek-harness/texpage-bridge/`. The portable Agent Skill + CLI adapter remains available as a separate fallback for runtimes that do not load the official dsh bundle format.

## Official DeepSeek Harness snapshot

The integration is pinned to `deepseek-ai/deepseek-harness` commit `141eb6fef83422698aef7a981029e843e8161534`, committed `2026-08-19T15:11:50Z`, corresponding to the `dsh 0.1.0-rc.8` release merge. The package versions checked at that snapshot are `@deepseek-ai/dsh-tools 0.1.0-rc.8`, `@deepseek-ai/cordis 4.0.1`, and `@deepseek-ai/schemastery 3.18.1`.

The following immutable official sources define the compatibility claim:

- The official README says DeepSeek Harness is developed by DeepSeek AI, uses an everything-is-a-plugin architecture powered by Cordis, and is in developer preview with expected compatibility-breaking changes: [README lines 5–11](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/README.md#L5-L11).
- The same README recommends adding the `dsh-plugin` repository topic for discoverability: [README lines 39–43](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/README.md#L39-L43).
- The architecture guide defines Cordis as the framework under dsh and describes profiles, bundles, `dsh.bundle`, and patch-layer composition: [architecture lines 9–27](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/architecture.md#L9-L27).
- The first-plugin tutorial defines the named `apply(ctx)` module convention: [plugin tutorial lines 15–29](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/user/develop/basic/index.md#L15-L29).
- The tool tutorial uses `inject = ['tools']` and `ctx.tools.register(defineTool(...))`: [tool tutorial lines 7–36](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/user/develop/basic/tool.md#L7-L36).
- The publishing guide specifies an installable package with a `dsh.bundle` patch and installation through `dsh plugin --profile ... add ...`: [bundle format lines 9–64](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/user/develop/basic/publish.md#L9-L64) and [profile installation lines 75–105](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/user/develop/basic/publish.md#L75-L105).
- Representative official package/plugin shapes checked alongside the tutorials were [`dsh-tool-todo`](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/todo/tool-todo/src/index.ts), [`dsh-mcp-client`](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/mcp/mcp-client/src/index.ts), and [`dsh-schedule`](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/schedule/schedule/src/index.ts). The current repository package/build rules were also checked against the pinned [development guide](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/development.md).

Because Harness is explicitly a developer preview, this is an exact-snapshot compatibility statement, not a promise that later release candidates or `master` commits retain the same package or tool ABI.

## Compatibility matrix

| Runtime / project | Verified surface | texpage-bridge status | Claim boundary |
| --- | --- | --- | --- |
| `deepseek-ai/deepseek-harness` `0.1.0-rc.8` at `141eb6fef83422698aef7a981029e843e8161534` | Official dsh bundle/profile install path; Cordis `apply(ctx)` plugin; `ctx.tools` + `defineTool` | **Official DeepSeek Harness plugin integration** in `integrations/deepseek-harness/texpage-bridge/` | Developer-preview snapshot only. Re-verify before changing the pinned versions. |
| `Hmbown/CodeWhale` `v0.9.1` (formerly DeepSeek-TUI) | `SKILL.md` discovery and local command execution | **Generic fallback** via `integrations/agent-skill/texpage-bridge/` | Format compatibility only; this is separate from the official DeepSeek Harness Cordis ABI. |
| `deepseek-ai/awesome-deepseek-agent` | DeepSeek-organization integration index / historical DeepSeek-TUI guidance | **Reference source only** | Documentation/indexing repository, not the dsh plugin host. |
| `HenryZ838978/deepseek-harness` `0.2.0` | Third-party Python/CLI/MCP/Skill packaging | **No direct binding**; generic Skill conventions may overlap | Do not confuse this third-party project with the official `deepseek-ai/deepseek-harness`. |
| `tylerbuilds/deepseek-harness` | Third-party CLI/MCP runtime | **No direct binding** | Separate project and protocol surface; the official dsh integration does not target it. |
| Other `SKILL.md`-aware agents | Common Markdown skill convention plus local Python execution | **Portable fallback by design** | Host-specific discovery, approval, sandbox, and subprocess behavior must be checked separately. |

## Official plugin capability boundary

The dsh package intentionally exposes only three model-facing tools:

1. `texpage_build` — submit an allow-listed project and wait for the build result;
2. `texpage_submit` — queue an asynchronous build for an allow-listed project;
3. `texpage_status` — read the last local build record after additional provider-field/URL scrubbing.

The plugin accepts only project aliases explicitly configured by the operator. It delegates to the already-existing low-privilege Python adapter through `execFile` without a shell. It does not expose `publish`, `request` enumeration, version administration, broker administration, project-registry enumeration, or credentials.

The generic Skill/CLI fallback keeps its slightly broader read-only request inspection surface (`request` and `requests`) because that interface predates the official plugin package. Both paths keep privileged TeXPage state behind the bridge/broker.

## `dsh-plugin` repository topic

The official Harness README recommends the [`dsh-plugin`](https://github.com/topics/dsh-plugin) topic for plugin repositories. texpage-bridge documentation therefore recommends adding that topic when publishing this integration. Repository topics are not modified by the plugin, install scripts, tests, or this compatibility work.

## NJU-specific backend assumptions

Both agent integrations are provider-neutral at their public request boundary, but the current bridge/broker implementation is not. The backend currently assumes NJU's TeXPage deployment in several places, including:

- editor and API base URLs under `https://tex.nju.edu.cn`;
- the Git credential host `git.tex.nju.edu.cn`;
- NJU project/version route shapes and API response fields;
- current compile-control DOM behavior;
- a persistent headed Chromium session because NJU's security layer may reject true headless operation;
- the present Git Credential Manager repair flow.

Another TeXPage-compatible deployment should keep the same low-privilege agent request contract while replacing provider-specific behavior behind the broker. A clean adaptation would make the web/API base URLs, Git host/auth strategy, project/version routes, response-field mapping, compile-control detection, and login/session validation provider configuration or a provider module. Do not move credentials or provider tokens into either agent integration package to make another deployment work.

## Security boundary

The integration packages must remain safe to publish. In particular they must not include or enumerate:

- `projects.json` / `projects.local.json`;
- `requests/` contents;
- broker logs or PID/runtime state;
- Chromium profiles or browser storage;
- TeXPage/Git credentials or tokens;
- signed PDF/log artifact URLs;
- private deployment endpoints learned from authenticated browser state.

The official dsh plugin's `status` path additionally strips `project_key`, `version_no`, URL fields, credential/token-like keys, and URL-like string values before returning the record to Harness. Authentication, browser state, signed result downloads, and all administrative operations stay behind the existing broker boundary.
