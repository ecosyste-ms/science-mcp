# Science MCP

A local Python MCP server for [ecosyste.ms Science](https://science.ecosyste.ms). It looks up candidate software in a Science SQLite snapshot and reads current descriptions, languages, package metadata, and dependencies from the Science API. Metadata extraction stays in Science.

## Run locally

Install Python and [uv](https://docs.astral.sh/uv/), then run from this checkout:

```sh
uv sync --locked
SCIENCE_SEEDS_DB=/absolute/path/to/search-seeds.sqlite3 uv run --locked science-mcp
```

The server communicates over stdio and waits for an MCP client. Set `SCIENCE_API_URL` to use another API base, such as a local Science instance. The default is `https://science.ecosyste.ms/api/v1`.

`SCIENCE_SEEDS_DB` accepts a Science export with schema version 1 or 2 and opens it read-only. Create one with Science's `search_seeds:export` rake task. Without a snapshot, the live API tools still work; lookup returns a configuration error. Nothing downloads the full catalogue at startup or during lookup.

## Tools

| Tool | What it returns |
| --- | --- |
| `lookup_software` | Exact matches for a name, DOI, repository URL, homepage URL, or canonical versionless PURL |
| `list_seed_projects` | One page of live search seeds, with the next page number |
| `get_project_context` | Live project summary, or a page of published packages or direct dependencies |

Start with `lookup_software(query="orbdot")`, then pass a returned `project_id` to `get_project_context`. Use `section="packages"` or `section="direct_dependencies"` for details. Follow `next_offset` with the same section and project.

Lookup returns evidence rows, so several rows can refer to the same project or package. Preserve those sources and any competing identities. Follow `next_after_id` using the same query, kind, and snapshot until it is null. Names use lowercase Unicode NFC; DOI URLs are accepted; URL paths retain case. Lookup does not perform fuzzy matching or normalize PURLs supplied by the caller.

Snapshot results include the snapshot ID, completion time, and selection rules, including any export limit. A missing match is not proof that software is absent from Science. Live responses include retrieval time and source timestamps, and can differ from an older snapshot. Direct dependencies are project-level observations; they do not imply that every package published by the project has those dependencies.

Tool pages contain at most 25 matches or context items, and seed pages at most 10 projects. Long text and nested arrays have explicit `truncations` entries. Serialized result data is limited to 64 KiB; a larger result returns an error asking for a smaller page. The API still returns the full context to the adapter before it selects a page. Requests have a 15-second socket timeout and a 10 MiB response limit.

## Connect a client

Replace the absolute paths below. Use the full path to `uv` if the client cannot find it. These commands register the server in the chosen client's configuration.

For [Claude Code](https://code.claude.com/docs/en/mcp):

```sh
claude mcp add --transport stdio --scope user science \
  --env SCIENCE_SEEDS_DB=/absolute/path/to/search-seeds.sqlite3 \
  -- uv run --directory /absolute/path/to/science-mcp --locked science-mcp
```

For [Codex](https://developers.openai.com/codex/mcp):

```sh
codex mcp add science \
  --env SCIENCE_SEEDS_DB=/absolute/path/to/search-seeds.sqlite3 \
  -- uv run --directory /absolute/path/to/science-mcp --locked science-mcp
```

Restart the client session, then ask it to look up OrbDot and retrieve its project context. Each client starts its own stdio server process with the same tools.

## Checks

```sh
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
SCIENCE_SEEDS_DB=/absolute/path/to/search-seeds.sqlite3 uv run --locked python scripts/smoke.py
```

The tests start stdio subprocesses and a local HTTP fixture server. The smoke script makes read-only requests to the configured Science API and, when configured, queries the local snapshot.

Released under the [MIT License](LICENSE).
