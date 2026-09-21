import json
import sqlite3
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_snapshot(path, version=2):
    with closing(sqlite3.connect(path)) as database:
        database.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE projects (project_id INTEGER PRIMARY KEY, repository_url TEXT NOT NULL);
            CREATE TABLE packages (id INTEGER PRIMARY KEY, project_id INTEGER, package_id INTEGER,
                                   purl TEXT, registry TEXT, ecosystem TEXT);
            CREATE TABLE seeds (id INTEGER PRIMARY KEY, project_id INTEGER,
                                package_entry_id INTEGER, type TEXT, value TEXT,
                                normalized_value TEXT, source TEXT, relation TEXT);
            CREATE INDEX seeds_lookup ON seeds(type, normalized_value);
            CREATE INDEX packages_purl ON packages(purl);
        """)
        database.execute(f"PRAGMA user_version = {version}")
        metadata = {
            "snapshot_id": "test-snapshot",
            "schema_version": version,
            "completed_at": "2026-09-21T12:00:00Z",
            "selection": {"visible": True, "minimum_science_score": 20, "limit": 2},
        }
        database.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in metadata.items()],
        )
        database.executemany(
            "INSERT INTO projects VALUES (?, ?)",
            [
                (1, "https://github.com/example/Stats"),
                (2, "https://github.com/fork/Stats"),
            ],
        )
        database.executemany(
            "INSERT INTO packages VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 1, 40, "pkg:pypi/stats", "pypi.org", "pypi"),
                (2, 2, None, "pkg:cran/stats", "cran.r-project.org", "cran"),
            ],
        )
        database.executemany(
            "INSERT INTO seeds VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 1, None, "name", "Stats", "stats", "project.name", "software"),
                (2, 2, None, "name", "Stats", "stats", "project.name", "software"),
                (3, 1, 1, "name", "stats", "stats", "package.name", "software"),
                (4, 2, 2, "name", "stats", "stats", "project.packages[0].name", "software"),
                (
                    5,
                    1,
                    None,
                    "doi",
                    "10.1234/Stats",
                    "10.1234/stats",
                    "joss_metadata.doi",
                    "publication",
                ),
                (6, 1, None, "name", "Café", "café", "project.name", "software"),
                (
                    7,
                    1,
                    None,
                    "repository_url",
                    "https://github.com/example/Stats",
                    "https://github.com/example/Stats",
                    "project.url",
                    "software",
                ),
                (8, 1, None, "name", "R++", "r++", "project.name", "software"),
            ],
        )
        database.commit()


def sample_context():
    return {
        "project_id": 1,
        "repository_url": "https://github.com/example/stats",
        "updated_at": "2026-09-21T12:00:00Z",
        "last_synced_at": "2026-09-20T12:00:00Z",
        "descriptions": [{"value": "Statistical tools", "source": "project.description"}],
        "languages": [{"value": "Python", "source": "repository.language"}],
        "packages": [
            {
                "package_id": 40,
                "purl": "pkg:pypi/stats",
                "names": [{"value": "stats", "source": "package.name"}],
            }
        ],
        "dependencies_indexed_at": "2026-09-20T12:00:00Z",
        "direct_dependencies": [
            {
                "dependency_id": index,
                "name": name,
                "package_id": None,
                "purl": None,
                "source": "repos_manifests",
                "ecosystem": "pypi",
                "occurrences": [
                    {"filepath": "requirements.txt", "requirements": ">=1", "optional": False}
                ],
            }
            for index, name in enumerate(["numpy", "scipy", "pandas"], start=1)
        ],
    }


class FakeAPI:
    def __init__(self):
        self.responses = {}
        self.requests = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                state.requests.append(self.path)
                status, headers, body = state.responses.get(self.path, (404, {}, b"not found"))
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
