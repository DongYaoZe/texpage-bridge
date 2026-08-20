# TeXPage cloud-build bridge

A local bridge that connects Git worktrees to a TeXPage-compatible cloud compiler through a persistent Chromium broker.

The bridge is designed for multi-agent editing workflows where source stays local, compilation happens remotely, and callers should not receive browser session data, Git credentials, signed artifact URLs, or other privileged state.

## Local configuration

`projects.json` is intentionally local-only and ignored by Git. It may contain machine-specific repository paths plus live TeXPage project/version identifiers.

Start from the sanitized template:

```bat
copy projects.example.json projects.json
```

Then replace the placeholder repository path, `project_key`, `version`, and `version_no` values with your local values. Project aliases are arbitrary local names such as `sample-project` or `second-project`.

Browser login state should live in a persistent Chromium profile outside this repository. Credentials, cookies, signed artifact URLs, request history, broker logs, and real project registries must not be committed.

## Requirements

- Python 3.11+
- Git
- Playwright with Chromium
- Windows Git Credential Manager for the current non-interactive Git credential flow

Install the Python dependency and browser runtime with:

```bat
python -m pip install -r requirements.txt
python -m playwright install chromium
```

`texpage.cmd` uses `TEXPAGE_BRIDGE_PYTHON` when set; otherwise it runs `python` from `PATH`.

The broker profile defaults to `%USERPROFILE%\.texpage-bridge\chromium-profile`. Override it with the local `profile` field in `projects.json` or the `TEXPAGE_BRIDGE_PROFILE` environment variable.

## Agent-facing build interface

Ordinary editing/review agents should use the low-privilege wrapper:

```bat
texpage-agent.cmd sample-project build
```

`build` is a submit-and-wait operation. The calling agent does not perform the privileged browser/Git workflow itself. The central service:

1. freezes the current worktree into a temporary root commit without touching the real branch or index;
2. queues the request and performs the TeXPage Git push centrally;
3. assigns the request to one broker-owned top-level Chromium worker window;
4. compiles and captures fresh result URLs internally;
5. downloads PDF/log centrally and exposes only sanitized artifact paths and QA metrics;
6. writes `.texpage/latest.pdf`, `.texpage/latest.log`, and `.texpage/build.json` in the target repository.

The root-commit snapshot prevents unrelated repository history and old large objects from being copied to the cloud-build branch.

For an asynchronous build:

```bat
texpage-agent.cmd sample-project submit
```

It returns a request id such as `tp-YYYYMMDD-HHMMSS-xxxxxxxx`. Later retrieve it with:

```bat
texpage-agent.cmd sample-project request tp-YYYYMMDD-HHMMSS-xxxxxxxx
texpage-agent.cmd sample-project requests --limit 20
```

Request state is persisted locally under `requests/`. If the broker restarts, unfinished requests are marked `interrupted` rather than silently remaining `running` forever.

Show the last successful local result without compiling:

```bat
texpage-agent.cmd sample-project status
```

### Official DeepSeek Harness plugin + generic Agent Skill fallback

An installable plugin for the **official** `deepseek-ai/deepseek-harness` is available under `integrations/deepseek-harness/texpage-bridge/`. It follows the current dsh bundle/Cordis conventions and registers only `texpage_build`, `texpage_submit`, and `texpage_status`. It was implemented against the developer-preview snapshot `deepseek-ai/deepseek-harness@141eb6fef83422698aef7a981029e843e8161534` (`dsh 0.1.0-rc.8`, commit dated 2026-08-19 UTC); see the package [README](integrations/deepseek-harness/texpage-bridge/README.md) for installation, configuration, pinned official-source citations, and the breaking-change warning.

The existing portable Agent Skill under `integrations/agent-skill/texpage-bridge/` remains the generic fallback for Skill-aware runtimes. It exposes `build`, `submit`, `request`, `requests`, and `status` through a cross-platform Python adapter while keeping administrative/versioning commands and broker controls outside the surface.

The older `texpage-agent.cmd` remains for backward-compatible Windows workflows and has a broader command allow-list. New DeepSeek Harness integrations should use the official dsh package; other runtimes can continue to use the narrow Skill adapter.

For the fallback adapter, set `TEXPAGE_BRIDGE_HOME` to this checkout, copy the skill directory into a location supported by the target runtime, and call for example:

```sh
python scripts/texpage_agent.py sample-project build
```

Neither integration contains a DeepSeek API client or TeXPage credentials. The official Harness compatibility boundary, generic fallback matrix, and NJU-specific provider assumptions that remain behind the broker are documented in [`COMPATIBILITY.md`](COMPATIBILITY.md).

## One browser, queued top-level window pool

The bridge uses one long-lived local broker on `127.0.0.1:43177` by default.

A multi-tab design was replaced after concurrent use showed that tabs could interfere with each other's UI state. The broker instead owns a fixed pool of real top-level Chromium windows created through CDP.

Current model:

- callers may submit builds concurrently;
- local snapshot creation and Git pushes may overlap across different repositories;
- the broker owns a fixed worker pool (`browser_workers`, default 4);
- each build request is bound to one worker window until PDF/log collection finishes;
- different configured projects may compile in parallel;
- excess requests wait in FIFO order;
- the same project is serialized by its project lock so two snapshots cannot race the same remote branch;
- worker windows remain headful but may be parked off-screen;
- PDF/log downloads remain isolated in each target repository's `.texpage/` directory.

Agents do not open or own broker Chromium windows.

Broker commands (any configured project alias may address the shared broker):

```bat
texpage.cmd sample-project broker start
texpage.cmd sample-project broker status
texpage.cmd sample-project broker stop
```

Normal `build` automatically starts the broker if needed.

## Non-interactive Git authentication

Central Git pushes never prompt an agent for credentials. The service uses non-interactive Git settings and checks remote authentication before a push.

If the stored host-level credential is missing or expired, the broker can use its existing authenticated TeXPage web session to obtain a fresh Git credential and store it through Git Credential Manager. The credential is passed via stdin only; it is not written to project configuration, request records, logs, or command-line arguments.

## Administrative controls

The full `texpage.cmd` interface remains available for maintenance and recovery:

```bat
texpage.cmd sample-project versions
texpage.cmd sample-project reserve-version --dry-run
texpage.cmd sample-project reserve-version
texpage.cmd sample-project set-version <version> <version_no>
texpage.cmd sample-project broker status
```

These are administrative commands. `texpage-agent.cmd` intentionally rejects `versions`, `reserve-version`, `set-version`, and `broker`.

## Formal publish workflow

A useful release discipline is:

> one formal source release corresponds to one newly reserved TeXPage version.

Request publication through the central service:

```bat
texpage-agent.cmd sample-project publish
```

`publish` performs the privileged sequence centrally:

1. freezes the current worktree into a root snapshot before queueing;
2. re-reads live versions and reserves the next unused semantic version;
3. updates the selected project/version mapping under a shared config lock;
4. pushes the frozen snapshot to the newly created remote version branch;
5. compiles, downloads PDF/log, and runs basic log QA;
6. returns a sanitized result without exposing browser state, credentials, signed URLs, or remote project internals.

If a newly reserved version compiles but source still needs fixes, do **not** reserve another version just to rebuild it. Fix the source and run:

```bat
texpage-agent.cmd sample-project build
```

The same rule applies when `publish` fails after a concrete `version` / `version_no` has already been persisted. Recover that selected version with ordinary `build` rather than issuing another `publish`.

## Reliability protections

The bridge includes two narrow protections derived from observed TeXPage behavior:

- a newly created version's Git ref can appear asynchronously while the first snapshot push is in flight; only the matching `cannot lock ref` / `reference already exists` race is retried with bounded backoff;
- compiled PDF/log artifacts may become visible after a propagation delay; the broker re-enters the exact version route and accepts only fresh post-compile artifact URLs, reducing stale-PDF and false missing-result failures.

Other Git, authentication, browser, and compile failures remain visible to the caller rather than being broadly retried.

## Concurrency model

```text
Agent A -- submit/publish --\
Agent B -- submit/publish ---+--> central service --> worker window 1
Agent C -- submit/publish ---+                  |--> worker window 2
Agent D -- submit/publish ---+                  |--> worker window 3
Agent E -- submit/publish --/                   `--> worker window 4
                                                 excess -> FIFO queue
```

Safe to issue concurrently:

- `build` / `submit` / `publish` requests for different configured repositories;
- up to the configured worker count of cloud compilations;
- artifact downloads to different repositories.

Automatically serialized or protected:

- two service jobs for the same project;
- version creation for the same project;
- shared `projects.json` updates;
- requests beyond worker capacity;
- request lookup, which is project-scoped.

Do not assign multiple agents to write the same local worktree concurrently. Snapshotting makes submission deterministic; it does not make arbitrary concurrent file edits safe.

## Login expiry

If the TeXPage web login expires:

1. stop the broker;
2. open the saved Chromium profile interactively;
3. log in once;
4. close that browser;
5. run any bridge command again so the broker restarts with the restored profile.

Do not put passwords, cookies, Git tokens, project identifiers, or signed artifact URLs into prompts, scripts, Git history, or issue text.

## Tests

Run the deterministic regression suite with:

```bat
python -m unittest discover -s tests -v
```

The tests cover the bounded Git ref-race retry and broker endpoint defaults without contacting TeXPage or modifying live project state.
