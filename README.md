# Vezlu

Keep two approaches, inspect their actual text, and combine the parts you choose.

Vezlu is a small local Claude companion for text artifacts: launch plans, research notes, decision briefs or code snippets. Claude supplies the alternatives. Vezlu stores, compares and combines them through a CLI or nine MCP tools. Each project has independent named branches and immutable revision snapshots in SQLite.

## Install

Requires Python 3.12+ on macOS or Linux. From this repository's directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/vezlu --help
```

The runtime dependency is the official MCP Python SDK, pinned to 2.3.0. No model account or API key is needed to run the CLI. Your Claude host has its own access requirements.

## Try a complete example

```sh
.venv/bin/vezlu --store "$PWD/.vezlu/demo" demo
.venv/bin/vezlu --store "$PWD/.vezlu/demo" call branches '{"project":"launch-demo"}'
.venv/bin/vezlu --store "$PWD/.vezlu/demo" call compare '{"project":"launch-demo","left":"community","right":"partners"}'
.venv/bin/vezlu --store "$PWD/.vezlu/demo" call read '{"project":"launch-demo","branch":"combined"}'
```

The demo uses **fixture text**, not model output or a performance benchmark. From one launch plan, `community` changes the brief to a free neighbourhood trial with a repeat-attendance goal; `partners` changes distribution to library and club partnerships. The JSON output shows both complete alternatives, exact differences and a third `combined` result containing the community brief and partner channels. The original alternatives remain available.

The demo creates `launch-demo` once. Repeating it in that store returns `project already exists`; choose a fresh private store path for another run. The default store, when `--store` is omitted, is `~/.local/share/vezlu`.

## Use with Claude

Add this entry to your Claude Desktop MCP configuration, replacing both paths with absolute paths on your machine:

```json
{
  "mcpServers": {
    "vezlu": {
      "command": "/absolute/path/to/vezlu/.venv/bin/vezlu",
      "args": ["--store", "/absolute/private/data/vezlu", "serve"]
    }
  }
}
```

Restart the host and enable the server. `serve` speaks MCP over stdio. The test suite checks the official SDK's subprocess handshake and tool calls; a live Claude Desktop session is a separate host setup check. [Local MCP server configuration](https://modelcontextprotocol.io/docs/develop/connect-local-servers) describes the host configuration. The adjacent [SKILL.md](SKILL.md) provides a focused workflow you can give Claude.

Try: “Create two approaches to my launch. Keep the same artifact keys on both branches. Show their differences, explain the tradeoffs, and wait for my choice before combining selected artifacts.”

## Operations

CLI calls use `vezlu [--store DIRECTORY] call OPERATION 'JSON_OBJECT'`. MCP tool names and arguments match the table. Results are JSON text; CLI errors return an `error` object and exit status 1. MCP input failures return tool errors. A merge conflict is a successful request with `ok: false`, and never changes the target.

| Operation | Arguments | Result |
| --- | --- | --- |
| `projects` | none | Saved project names |
| `create` | `project` | Empty `main` branch and `head` |
| `branches` | `project` | Names and current heads |
| `branch` | `project`, `branch`, `source`, `expected_head` | New branch at source head |
| `read` | `project`, `branch`, optional `revision` | Head, selected revision and all artifacts |
| `write` | `project`, `branch`, `changes`, `expected_head` | New head; changes map keys to text, or `null` to delete |
| `history` | `project`, `branch`, optional `limit` (1–200, default 50) | Newest reachable revision IDs, parents and merge source |
| `compare` | `project`, `left`, `right` | Both heads and sorted changes with exact before/after text and unified diffs |
| `merge` | `project`, `source`, `target`, `paths`, `expected_source`, `expected_target` | `ok`, head, common base and structured conflicts |

Read current heads before a write, fork or merge, then send those exact tokens. A stale token refuses the operation: reread the branch, inspect the new content and reconsider the choice. Multiple artifact changes commit atomically.

## Combination rules

Combination works on **whole artifacts**, without line-level merging. For each selected key, Vezlu finds the unique closest common ancestor of the branch heads, then:

- Adopts source changes if the target still matches the base, including additions and deletions.
- Retains target-only changes and accepts identical edits.
- Returns base, source and target values when both branches changed an artifact differently. All selected changes are withheld if any conflict occurs.

Choose conflicting text manually and save it with `write`; there is no automatic winner. Unrelated or ambiguous ancestry is refused. A partial combination records its source reference but only records the source as a second ancestry parent when all its changes are present in the result. This keeps unselected source changes available later. Repeated edits after a partial combination can conservatively conflict and need manual choice.

Branching, version history, unified diffs and three-way merging are established version-control techniques. Vezlu applies them to a small local text workflow. It does not measure which alternative is better.

## Storage and limits

Artifacts are logical keys, never imported files or filesystem paths. Project and branch names use 1–64 ASCII letters, digits, dots, underscores or hyphens and start with a letter or digit. Artifact keys allow similarly named slash-separated segments, up to 200 characters. Text must be valid UTF-8, at most 64 KiB per artifact, 128 artifacts and 1 MiB of text per snapshot. History accumulates on disk; there is no pruning in this version.

The store contains **plaintext**. Use a private directory on a local filesystem. Vezlu creates it with mode `0700` and the database with `0600`; unsafe existing permissions, symlink path components, symlink database sidecars and hardlinked databases are refused. On macOS, `/tmp` is a symlink: use a real absolute directory, such as one under your home. These checks protect against accidental redirection; they are not a boundary against another process acting as your own user.

There is no cloud sync, model invocation, HTTP listener, shell-command tool, filesystem import/export, hook or telemetry. Outputs appear only in CLI/MCP responses. Keep secrets out of artifacts. Back up the store only while all Vezlu processes are stopped. Windows and network filesystems are not supported in this release.

## Check the source

After installation, from the checkout:

```sh
.venv/bin/python -m unittest -v test_vezlu
```

The tests cover isolated branches, immutable history, restarts, independent and conflicting combinations, deletion, partial ancestry, concurrent stale writers, malformed input, private paths, real SDK stdio negotiation and the CLI fixture demo. MIT licensed; see [LICENSE](LICENSE).
