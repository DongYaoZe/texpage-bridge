# texpage-bridge for official DeepSeek Harness

This package is the official DeepSeek Harness (`dsh`) integration for texpage-bridge. It is an installable dsh **bundle** containing a Cordis plugin that registers three model-facing tools:

- `texpage_build` — submit the current allow-listed worktree and wait for the compile result;
- `texpage_submit` — submit an asynchronous build request;
- `texpage_status` — read the last local sanitized build record without compiling.

It deliberately does not expose `publish`, version creation/selection, broker start/stop, credentials, browser state, project registry enumeration, or signed artifact URLs.

## Compatibility snapshot

DeepSeek Harness is in developer preview and explicitly warns that compatibility-breaking changes are expected. This package was implemented and contract-tested against:

- `deepseek-ai/deepseek-harness` commit `141eb6fef83422698aef7a981029e843e8161534`
- commit date `2026-08-19T15:11:50Z`
- `dsh` / `@deepseek-ai/dsh-tools` `0.1.0-rc.8`
- `@deepseek-ai/cordis` `4.0.1`
- `@deepseek-ai/schemastery` `3.18.1`

Pinned official sources used for this integration:

- [Harness README: developed by DeepSeek AI, everything-is-a-plugin, developer-preview warning](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/README.md#L5-L11)
- [Harness README: `dsh-plugin` repository-topic recommendation](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/README.md#L39-L43)
- [Architecture: Cordis, profiles, bundles, and `dsh.bundle`](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/architecture.md#L9-L27)
- [Plugin tutorial: named `apply(ctx)` plugin convention](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/user/develop/basic/index.md#L15-L29)
- [Tool tutorial: `inject = ['tools']` and `ctx.tools.register(defineTool(...))`](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/user/develop/basic/tool.md#L7-L36)
- [Publishing guide: installable bundle manifest and package-referenced patch row](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/user/develop/basic/publish.md#L9-L64)
- [Publishing guide: `dsh plugin --profile ... add ...` profile installation](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/user/develop/basic/publish.md#L75-L105)
- [Representative official model-facing tool plugin: `dsh-tool-todo`](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/todo/tool-todo/src/index.ts)
- [Representative official bridge plugin: `dsh-mcp-client`](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/mcp/mcp-client/src/index.ts)
- [Representative official lifecycle plugin: `dsh-schedule`](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/packages/schedule/schedule/src/index.ts)
- [Development guide used to check the current package/build conventions](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/development.md)

Re-verify these assumptions before updating the pinned Harness versions.

## Install into a dsh profile

The package is plain ESM JavaScript, so a local checkout does not need a build or install-time `prepare` script.

From the texpage-bridge checkout:

```sh
dsh plugin --profile demo add ./integrations/deepseek-harness/texpage-bridge
```

The bundle inserts the `texpage-bridge` Cordis row. Its default `projects: []` intentionally exposes no project until the operator adds an allow-list in the profile's later `cordis.patch.yml` layer. Harness patches replace the targeted row's entire `config`, so restate every option you want to retain:

```yaml
- id: texpage-bridge
  config:
    bridgeHome: 'D:\path\to\texpage-bridge'
    pythonExecutable: 'python'
    projects: [sample-project]
    timeoutSeconds: 240
    maxOutputBytes: 262144
```

`bridgeHome` may instead be left empty when `TEXPAGE_BRIDGE_HOME` points at the checkout. `pythonExecutable` may be left empty to use `TEXPAGE_BRIDGE_PYTHON`, then the platform default (`python` on Windows, `python3` elsewhere).

Inspect the composed layer without starting the app:

```sh
dsh --profile demo --dump-config
```

Then start that profile normally. The plugin declares `inject = ['tools']`, so Cordis activates it only after Harness's tool registry exists.

## Security boundary

The plugin does not implement TeXPage authentication and does not read the private project registry. It validates the configured project allow-list and delegates only `build`, `submit`, or `status` to the existing generic low-privilege adapter at `integrations/agent-skill/texpage-bridge/scripts/texpage_agent.py` using `execFile` without a shell.

The existing bridge/broker remains responsible for Git snapshotting, TeXPage project/version identifiers, login state, browser automation, credentials, signed artifact downloads, and sanitized local artifacts. `texpage_status` additionally removes provider identifiers and token/URL-like fields from the JSON it returns to Harness.

## Development and contract tests

Install the pinned public Harness packages and run the package tests:

```sh
npm install --ignore-scripts --no-package-lock
npm test
```

The contract harness uses temporary fake bridge directories and a fake process runner. It verifies the bundle metadata, exact three-command allow-list, subprocess argument boundary, and status sanitization without launching the broker or performing a real TeXPage mutation.

The DeepSeek Harness README recommends the `dsh-plugin` GitHub repository topic for plugin discoverability. This repository's documentation recommends that topic as well; installation or tests do not change repository topics.
