# Science MCP

A local Python MCP server for [ecosyste.ms Science](https://science.ecosyste.ms). It searches software names, looks up identifiers, and reads descriptions, languages, package metadata, and dependencies through the Science API. Metadata extraction stays in Science.

## Run locally

Install Python and [uv](https://docs.astral.sh/uv/), then run from this checkout:

```sh
uv sync --locked
uv run --locked science-mcp
```

The server communicates over stdio and waits for an MCP client. Set `SCIENCE_API_URL` to use another API base, such as a local Science instance. The default is `https://science.ecosyste.ms/api/v1`.

No local database or catalogue download is required. API lookup and search use Science's `/software/lookup` and `/software/search` endpoints; the service operator must deploy these routes and populate the search fields before they can return results. An HTTP 404 from either route means it is unavailable on the configured service.

## Tools

| Tool | What it returns |
| --- | --- |
| `lookup_software` | Exact matches for a name, DOI, repository URL, homepage URL, or PURL |
| `search_software` | Names containing at least three characters, including aliases and package names |
| `list_seed_projects` | One page of live search seeds, with the next page number |
| `get_project_context` | Live project summary, or a page of published packages or direct dependencies |

Start with `lookup_software(query="orbdot")` or `search_software(query="orbit")`, then pass a returned `project_id` to `get_project_context`. Use `section="packages"` or `section="direct_dependencies"` for details. Follow `next_offset` with the same section and project.

API lookup and search return matching evidence grouped in `projects`. Preserve those sources and any competing identities. Follow `next_after_id` using the same query and kind until it is null; the API cursor is a project ID. Names use lowercase Unicode NFC, DOI URLs are accepted, and URL paths retain case. API PURL lookup removes versions and subpaths. Search matches literal substrings without fuzzy matching or confidence scores.

A missing match is not proof that software is absent from Science. Live responses include retrieval time and source timestamps; index updates are asynchronous and pagination can change as metadata changes. Direct dependencies are project-level observations and do not imply that every package published by the project has those dependencies.

Tool pages contain at most 25 matches or context items, and seed pages at most 10 projects. Long text and nested arrays have explicit `truncations` entries. Serialized result data is limited to 64 KiB; a larger result returns an error asking for a smaller page. The API still returns the full context to the adapter before it selects a page. Requests have a 15-second socket timeout and a 10 MiB response limit.

## Connect a client

Replace the absolute paths below. Use the full path to `uv` if the client cannot find it. These commands register the server in the chosen client's configuration.

For [Claude Code](https://code.claude.com/docs/en/mcp):

```sh
claude mcp add --transport stdio --scope user science \
  -- uv run --directory /absolute/path/to/science-mcp --locked science-mcp
```

For [Codex](https://developers.openai.com/codex/mcp):

```sh
codex mcp add science \
  -- uv run --directory /absolute/path/to/science-mcp --locked science-mcp
```

Restart the client session, then ask it to look up OrbDot and retrieve its project context. Each client starts its own stdio server process with the same tools.

## Optional local snapshot

Set `SCIENCE_SEEDS_DB` only if you want `lookup_software` to read a fixed SQLite export instead of the API. Search and context tools still use the live API. There is no published snapshot download yet; creating an export requires a [Science checkout](https://github.com/ecosyste-ms/science) with a populated local PostgreSQL database and its Ruby dependencies installed. See [Science development setup](https://github.com/ecosyste-ms/science/blob/main/DEVELOPMENT.md) and the [export guide](https://github.com/ecosyste-ms/science/blob/main/docs/search-seeds.md#local-sqlite-export).

From the Science checkout, using its rbenv Ruby installation:

```sh
RBENV_VERSION="$(cat .ruby-version)" /opt/homebrew/bin/rbenv exec bundle exec ruby --version
OUTPUT=tmp/search-seeds.sqlite3 RBENV_VERSION="$(cat .ruby-version)" /opt/homebrew/bin/rbenv exec bundle exec rake search_seeds:export
```

The task prints a JSON summary with the output path and counts. It refuses to overwrite an existing file. Add `LIMIT=1000` for a small preview, or omit it for the full eligible catalogue. An empty local Science database produces no useful lookup data; the MCP server does not import the public catalogue into it.

Keep the completed file at a stable path, then verify it from the MCP checkout:

```sh
mkdir -p "$HOME/.local/share/science"
cp /absolute/path/to/science/tmp/search-seeds.sqlite3 "$HOME/.local/share/science/search-seeds.sqlite3"
SCIENCE_SEEDS_DB="$HOME/.local/share/science/search-seeds.sqlite3" uv run --locked python scripts/smoke.py
```

For a client, add `--env SCIENCE_SEEDS_DB=/absolute/path/to/search-seeds.sqlite3` before `--` in its registration command above and restart the client session. Both schema versions 1 and 2 are supported, and the file is opened read-only. A configured but missing or invalid file returns an error instead of switching sources.

Snapshot lookup returns `matches` as evidence rows, with a row ID cursor, snapshot ID, completion time, and selection rules. Several rows may describe the same project. Keep the same file throughout pagination; snapshot PURLs must be supplied in canonical versionless form. To refresh, export to a new filename, update the client's configured path, and restart its session. Unset `SCIENCE_SEEDS_DB` to return to API lookup.

## Checks

```sh
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
uv run --locked python scripts/smoke.py
```

The tests start stdio subprocesses and a local HTTP fixture server. CI runs the suite on Python 3.11 through 3.14, with Ruff and zizmor checks. Dependabot checks Python dependencies and GitHub Actions weekly. The smoke script makes read-only requests to the configured Science API and, when configured, queries the local snapshot.

Released under the [MIT License](LICENSE).
