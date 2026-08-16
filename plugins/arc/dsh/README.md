# ARC for DeepSeek Harness

This directory contains the thin DeepSeek Harness adapter for ARC. It
registers the existing ARC Skill and keeps the Skill directory as its resource
base. ARC's Python packages, runtime launcher, manuals, rules, and workflows
remain unchanged.

Install the released plugin from npm:

```bash
dsh plugin --profile web add @troywjz/arc-dsh
```

Or install the pinned GitHub release directly:

```bash
dsh plugin --profile web add github:troywjz/arc#v0.1.1
```

For local development, install this checkout into a dedicated DSH profile:

```bash
dsh plugin --profile arc add /path/to/arc
dsh --profile arc --dump-config
```

For the one-shot headless application, install it into that profile instead:

```bash
dsh plugin --profile headless add @troywjz/arc-dsh
dsh --profile headless "Use ARC to summarize arXiv:0911.3380"
```

The adapter targets Linux, macOS, and WSL because ARC's lazy runtime launcher
is a Bash script. It registers the ARC Skill, exposes ARC's command wrappers
through the package `bin` map, and starts a local authenticated Unix-socket
bridge for ARC's `dsh` provider. The bridge delegates model generation to the
native DSH `ctx.llm.prepareCall().stream()` path, so ARC does not need to own
or copy the DSH provider credentials.

When a DSH model shell is active, use `$DSH_ARC_RUNTIME` for ARC commands if a
bare command is not found. The bridge endpoint is advertised through
`$DSH_ARC_LLM_SOCKET` and `$DSH_ARC_LLM_TOKEN_FILE`. The bundled ARC runtime
is pinned to the corresponding immutable tag in `troywjz/arc`, so the npm and
GitHub installations use the fork's `dsh` provider implementation.
