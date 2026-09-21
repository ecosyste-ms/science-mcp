import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager, closing
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from support import FakeAPI, make_snapshot, sample_context

ROOT = Path(__file__).resolve().parents[1]


class StdioTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "seeds.sqlite3"
        make_snapshot(self.path)
        self.api = FakeAPI()
        self.addCleanup(self.api.close)
        self.api.responses["/api/v1/projects/1/search_context"] = (200, {}, sample_context())

    @asynccontextmanager
    async def client(self, **environment):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "science_mcp.server"],
            cwd=str(ROOT),
            env={
                "SCIENCE_API_URL": self.api.url,
                "SCIENCE_SEEDS_DB": str(self.path),
                **environment,
            },
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=10) as client:
                await client.initialize()
                yield client

    async def call(self, client, name, arguments):
        result = await client.call_tool(name, arguments)
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(result.structured_content, json.loads(result.content[0].text))
        return result.structured_content

    async def test_tools_are_discoverable_and_read_only(self):
        async with self.client() as client:
            tools = (await client.list_tools()).tools
            self.assertEqual(
                {"lookup_software", "search_software", "list_seed_projects", "get_project_context"},
                {tool.name for tool in tools},
            )
            for tool in tools:
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)

    async def test_live_seed_page_preserves_provenance_and_pagination(self):
        seeds = [
            {
                "project_id": 1,
                "seeds": [
                    {
                        "type": "name",
                        "value": "Stats",
                        "source": "project.name",
                        "relation": "software",
                    }
                ],
            }
        ]
        self.api.responses["/api/v1/projects/search_seeds?page=2&per_page=1"] = (
            200,
            {"Link": '<https://science.example/api/v1/projects/search_seeds?page=3>; rel="next"'},
            seeds,
        )
        async with self.client() as client:
            data = await self.call(client, "list_seed_projects", {"page": 2, "per_page": 1})
        self.assertEqual(seeds, data["projects"])
        self.assertEqual(3, data["next_page"])
        self.assertEqual("api", data["source"])
        self.assertIn("retrieved_at", data)
        self.assertEqual(["/api/v1/projects/search_seeds?page=2&per_page=1"], self.api.requests)

    async def test_context_summary_and_detail_pages(self):
        async with self.client() as client:
            data = await self.call(client, "get_project_context", {"project_id": 1})
            self.assertEqual("Python", data["languages"][0]["value"])
            self.assertEqual(3, data["direct_dependency_count"])
            self.assertEqual(1, data["package_count"])
            self.assertNotIn("direct_dependencies", data)
            data = await self.call(
                client,
                "get_project_context",
                {
                    "project_id": 1,
                    "section": "direct_dependencies",
                    "offset": 1,
                    "limit": 1,
                },
            )
            self.assertEqual("scipy", data["items"][0]["name"])
            self.assertEqual("requirements.txt", data["items"][0]["occurrences"][0]["filepath"])
            self.assertEqual(2, data["next_offset"])
            data = await self.call(
                client,
                "get_project_context",
                {
                    "project_id": 1,
                    "section": "direct_dependencies",
                    "offset": 3,
                },
            )
            self.assertEqual([], data["items"])
            self.assertIsNone(data["next_offset"])
            data = await self.call(
                client,
                "get_project_context",
                {
                    "project_id": 1,
                    "section": "packages",
                },
            )
            self.assertEqual("pkg:pypi/stats", data["items"][0]["purl"])
            self.assertIsNone(data["next_offset"])

    async def test_unknown_dependency_coverage_stays_unknown(self):
        context = sample_context()
        context.update(dependencies_indexed_at=None, direct_dependencies=[])
        self.api.responses["/api/v1/projects/1/search_context"] = (200, {}, context)
        async with self.client() as client:
            data = await self.call(client, "get_project_context", {"project_id": 1})
        self.assertIsNone(data["dependencies_indexed_at"])
        self.assertEqual(0, data["direct_dependency_count"])

    async def test_ambiguous_matches_are_paginated_without_losing_identity_or_evidence(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        async with self.client() as client:
            first = await self.call(client, "lookup_software", {"query": "STATS", "limit": 2})
            second = await self.call(
                client,
                "lookup_software",
                {
                    "query": "STATS",
                    "limit": 2,
                    "after_id": first["next_after_id"],
                },
            )
        self.assertEqual([1, 2], [row["project_id"] for row in first["matches"]])
        self.assertEqual([40, None], [row["package_id"] for row in second["matches"]])
        self.assertEqual(
            ["pkg:pypi/stats", "pkg:cran/stats"], [row["purl"] for row in second["matches"]]
        )
        self.assertEqual("project.packages[0].name", second["matches"][1]["source"])
        self.assertIsNone(second["next_after_id"])
        self.assertEqual("test-snapshot", first["snapshot_id"])
        self.assertEqual(2, first["selection"]["limit"])
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual([], self.api.requests)

    async def test_identifier_and_name_normalization(self):
        async with self.client() as client:
            queries = [
                ({"query": " CAFE\u0301 "}, "café"),
                ({"query": "R++"}, "r++"),
                ({"query": "https://doi.org/10.1234/STATS", "kind": "doi"}, "10.1234/stats"),
                (
                    {"query": "https://GITHUB.COM:443/example/Stats", "kind": "repository_url"},
                    "https://github.com/example/Stats",
                ),
                ({"query": "pkg:cran/stats", "kind": "purl"}, "pkg:cran/stats"),
            ]
            for arguments, expected in queries:
                with self.subTest(arguments=arguments):
                    data = await self.call(client, "lookup_software", arguments)
                    self.assertEqual(expected, data["query"])
                    self.assertEqual(1, len(data["matches"]))
            data = await self.call(
                client,
                "lookup_software",
                {
                    "query": "https://github.com/example/stats",
                    "kind": "repository_url",
                },
            )
            self.assertEqual([], data["matches"])
            data = await self.call(client, "lookup_software", {"query": "' OR 1=1 --"})
            self.assertEqual([], data["matches"])

    async def test_schema_version_one_is_supported(self):
        with closing(sqlite3.connect(self.path)) as database:
            database.execute("PRAGMA user_version = 1")
        async with self.client() as client:
            data = await self.call(client, "lookup_software", {"query": "stats"})
        self.assertEqual(1, data["schema_version"])

    async def test_lookup_uses_the_api_without_a_snapshot(self):
        path = "/api/v1/software/lookup?q=STATS&kind=name&limit=1&after_id=0"
        page = {
            "query": "stats",
            "kind": "name",
            "match": "exact",
            "next_after_id": 12,
            "projects": [
                {"project_id": 12, "seeds": [{"value": "Stats", "source": "project.name"}]}
            ],
        }
        self.api.responses[path] = (200, {}, page)
        next_path = "/api/v1/software/lookup?q=STATS&kind=name&limit=1&after_id=12"
        self.api.responses[next_path] = (200, {}, {**page, "projects": [], "next_after_id": None})
        async with self.client(SCIENCE_SEEDS_DB="") as client:
            first = await self.call(client, "lookup_software", {"query": "STATS", "limit": 1})
            self.assertEqual("api", first["source"])
            self.assertEqual(page["projects"], first["projects"])
            self.assertIn("retrieved_at", first)
            second = await self.call(
                client,
                "lookup_software",
                {"query": "STATS", "limit": 1, "after_id": first["next_after_id"]},
            )
            self.assertIsNone(second["next_after_id"])
        self.assertEqual([path, next_path], self.api.requests)

    async def test_name_search_always_uses_the_api_and_encodes_query(self):
        path = "/api/v1/software/search?q=R%2B%2B&limit=10&after_id=0"
        self.api.responses[path] = (
            200,
            {},
            {
                "query": "r++",
                "kind": "name",
                "match": "contains",
                "next_after_id": None,
                "projects": [{"project_id": 12}],
            },
        )
        async with self.client() as client:
            data = await self.call(client, "search_software", {"query": "R++"})
        self.assertEqual("api", data["source"])
        self.assertEqual([12], [item["project_id"] for item in data["projects"]])
        self.assertEqual([path], self.api.requests)

    async def test_live_lookup_errors_are_reported_without_snapshot_fallback(self):
        path = "/api/v1/software/lookup?q=stats&kind=name&limit=10&after_id=0"
        async with self.client(SCIENCE_SEEDS_DB="") as client:
            for status, body, error in [
                (404, {}, "not_found"),
                (429, {}, "rate_limited"),
                (200, [], "invalid_response"),
                (200, {"projects": [], "next_after_id": "bad"}, "invalid_response"),
            ]:
                self.api.responses[path] = (status, {}, body)
                result = await client.call_tool("lookup_software", {"query": "stats"})
                self.assertTrue(result.is_error)
                self.assertEqual(error, result.structured_content["error"])

    async def test_missing_snapshot_path_does_not_create_a_file(self):
        path = str(Path(self.directory.name) / "missing.sqlite3")
        async with self.client(SCIENCE_SEEDS_DB=path) as client:
            result = await client.call_tool("lookup_software", {"query": "stats"})
        self.assertTrue(result.is_error)
        self.assertEqual("snapshot_not_found", result.structured_content["error"])
        self.assertFalse(Path(path).exists())

    async def test_invalid_snapshot_reports_an_error(self):
        self.path.write_text("not sqlite")
        async with self.client() as client:
            result = await client.call_tool("lookup_software", {"query": "stats"})
        self.assertTrue(result.is_error)
        self.assertEqual("invalid_snapshot", result.structured_content["error"])

    async def test_http_failures_are_tool_errors_and_redirects_are_not_followed(self):
        async with self.client() as client:
            for status, code in [
                (404, "not_found"),
                (429, "rate_limited"),
                (500, "api_error"),
                (302, "api_error"),
            ]:
                with self.subTest(status=status):
                    self.api.responses["/api/v1/projects/1/search_context"] = (
                        status,
                        {"Retry-After": "30", "Location": self.api.url + "/redirected"},
                        b"error",
                    )
                    result = await client.call_tool("get_project_context", {"project_id": 1})
                    self.assertTrue(result.is_error)
                    self.assertEqual(code, result.structured_content["error"])
                    if status == 429:
                        self.assertIn("30", result.structured_content["message"])
        self.assertNotIn("/api/v1/redirected", self.api.requests)

    async def test_invalid_json_and_wrong_response_shapes_are_tool_errors(self):
        async with self.client() as client:
            for body in [b"<html>error</html>", [], {"project_id": 2}, {"project_id": 1}]:
                self.api.responses["/api/v1/projects/1/search_context"] = (200, {}, body)
                result = await client.call_tool("get_project_context", {"project_id": 1})
                self.assertTrue(result.is_error)
                self.assertEqual("invalid_response", result.structured_content["error"])

    async def test_invalid_inputs_never_reach_the_api(self):
        async with self.client() as client:
            for tool, arguments in [
                ("get_project_context", {"project_id": True}),
                ("get_project_context", {"project_id": "../lookup"}),
                ("get_project_context", {"project_id": 1, "limit": 26}),
                ("get_project_context", {"project_id": 1, "section": "readme"}),
                ("get_project_context", {"project_id": 1, "offset": -1}),
                ("list_seed_projects", {"page": 0}),
                ("list_seed_projects", {"per_page": 100}),
                ("lookup_software", {"query": "stats", "kind": "sql"}),
                ("search_software", {"query": "st"}),
            ]:
                with self.subTest(arguments=arguments):
                    result = await client.call_tool(tool, arguments)
                    self.assertTrue(result.is_error)
        self.assertEqual([], self.api.requests)

    async def test_long_evidence_is_explicitly_truncated(self):
        context = sample_context()
        context["descriptions"][0]["value"] = "é" * 3000
        self.api.responses["/api/v1/projects/1/search_context"] = (200, {}, context)
        async with self.client() as client:
            data = await self.call(client, "get_project_context", {"project_id": 1})
        self.assertEqual(2000, len(data["descriptions"][0]["value"]))
        self.assertEqual("project.description", data["descriptions"][0]["source"])
        self.assertEqual("$.descriptions[0].value", data["truncations"][0]["path"])
        self.assertEqual(3000, data["truncations"][0]["original_length"])

    async def test_large_results_return_an_actionable_size_error(self):
        context = sample_context()
        context["packages"] = [{"description": "x" * 2000, "extra": "y" * 2000}] * 25
        self.api.responses["/api/v1/projects/1/search_context"] = (200, {}, context)
        async with self.client() as client:
            result = await client.call_tool(
                "get_project_context",
                {
                    "project_id": 1,
                    "section": "packages",
                    "limit": 25,
                },
            )
            self.assertTrue(result.is_error)
            self.assertEqual("result_too_large", result.structured_content["error"])
            data = await self.call(
                client,
                "get_project_context",
                {
                    "project_id": 1,
                    "section": "packages",
                    "limit": 1,
                },
            )
            self.assertEqual(1, data["next_offset"])


if __name__ == "__main__":
    unittest.main()
