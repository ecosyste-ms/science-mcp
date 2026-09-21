import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .errors import ScienceError


class Snapshot:
    def __init__(self, path: str | None = None):
        self.path = path

    def lookup(self, query: str, kind: str, limit: int, after_id: int) -> dict:
        if not self.path:
            raise ScienceError(
                "snapshot_not_configured", "Set SCIENCE_SEEDS_DB to a Science SQLite export"
            )
        path = Path(self.path).expanduser().resolve()
        if not path.is_file():
            raise ScienceError(
                "snapshot_not_found", "SCIENCE_SEEDS_DB must point to an existing file"
            )
        value = self.normalize(query, kind)
        try:
            with closing(
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
            ) as database:
                database.row_factory = sqlite3.Row
                version = database.execute("PRAGMA user_version").fetchone()[0]
                if version not in (1, 2):
                    raise ScienceError(
                        "unsupported_snapshot", "Expected Science export schema 1 or 2"
                    )
                metadata = {
                    row["key"]: json.loads(row["value"])
                    for row in database.execute(
                        "SELECT key, value FROM metadata "
                        "WHERE key IN ('snapshot_id', 'completed_at', 'selection')"
                    )
                }
                if kind == "purl":
                    rows = database.execute(
                        """
                        SELECT k.id AS match_id, k.project_id, p.repository_url,
                               k.id AS package_entry_id, k.package_id, k.purl,
                               k.registry, k.ecosystem
                        FROM packages k JOIN projects p USING (project_id)
                        WHERE k.purl = ? AND k.id > ? ORDER BY k.id LIMIT ?
                    """,
                        (value, after_id, limit + 1),
                    ).fetchall()
                else:
                    rows = database.execute(
                        """
                        SELECT s.id AS match_id, s.project_id, p.repository_url,
                               k.package_id, k.purl, k.registry, k.ecosystem,
                               s.package_entry_id, s.type, s.value, s.normalized_value,
                               s.source, s.relation
                        FROM seeds s JOIN projects p USING (project_id)
                        LEFT JOIN packages k ON k.id = s.package_entry_id
                        WHERE s.type = ? AND s.normalized_value = ? AND s.id > ?
                        ORDER BY s.id LIMIT ?
                    """,
                        (kind, value, after_id, limit + 1),
                    ).fetchall()
                matches = [dict(row) for row in rows[:limit]]
                return {
                    "source": "snapshot",
                    "schema_version": version,
                    "snapshot_id": metadata["snapshot_id"],
                    "completed_at": metadata["completed_at"],
                    "selection": metadata["selection"],
                    "query": value,
                    "kind": kind,
                    "matches": matches,
                    "next_after_id": matches[-1]["match_id"] if len(rows) > limit else None,
                }
        except (sqlite3.Error, json.JSONDecodeError, KeyError) as error:
            raise ScienceError("invalid_snapshot", "Not a readable Science seed export") from error

    def normalize(self, query: str, kind: str) -> str:
        value = query.strip()
        if not value:
            raise ScienceError("invalid_query", "Query must not be blank")
        if kind == "name":
            return unicodedata.normalize("NFC", value).lower()
        if kind == "doi":
            return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I).lower()
        if kind in {"repository_url", "homepage_url"}:
            try:
                uri = urlsplit(value)
                if uri.scheme not in {"http", "https"} or not uri.hostname or "@" in uri.netloc:
                    raise ValueError
                host = uri.hostname.lower()
                if ":" in host:
                    host = f"[{host}]"
                if uri.port and (uri.scheme, uri.port) not in {("http", 80), ("https", 443)}:
                    host += f":{uri.port}"
                return urlunsplit((uri.scheme, host, uri.path, uri.query, uri.fragment))
            except ValueError as error:
                raise ScienceError(
                    "invalid_query", "Use a complete HTTP(S) URL without credentials"
                ) from error
        return value
