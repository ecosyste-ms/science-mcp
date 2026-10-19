import asyncio
import hashlib
import json
import os
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
                {
                    "lookup_software",
                    "search_software",
                    "list_seed_projects",
                    "get_project_context",
                    "get_project_external_identifiers",
                    "get_project_swhids",
                    "bulk_lookup_projects",
                    "bulk_lookup_packages",
                    "list_packages",
                    "list_package_versions",
                    "get_package_version",
                    "get_package_repositories",
                    "search_research_organizations",
                    "get_research_organization",
                    "list_research_accounts",
                },
                {tool.name for tool in tools},
            )
            for tool in tools:
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)

    async def test_collection_tools_preserve_evidence_filters_and_pagination(self):
        cases = [
            (
                "get_project_external_identifiers",
                {"project_id": 12},
                "projects/12/external_identifiers",
                "",
                {
                    "identifier": "ascl:example",
                    "match_status": "ambiguous",
                    "evidence": [{"source": "ascl"}],
                },
            ),
            (
                "list_package_versions",
                {"package_id": 40},
                "packages/40/versions",
                "",
                {"id": 7, "number": "1.0", "fetched_at": "2026-10-19T00:00:00Z"},
            ),
            (
                "get_package_repositories",
                {"package_id": 40},
                "packages/40/repositories",
                "",
                {
                    "id": 12,
                    "relationships": [
                        {
                            "type": "paper",
                            "role": "model_paper",
                            "package_evidence": [{"source": "model_card"}],
                        }
                    ],
                },
            ),
            (
                "list_packages",
                {
                    "ecosystem": "cran",
                    "domain": "physical-sciences",
                    "field": "physics",
                    "sort": "science_score",
                },
                "packages",
                "&ecosystem=cran&domain=physical-sciences&field=physics&sort=science_score",
                {"id": 40, "purl": "pkg:cran/stats", "rankings": {"downloads": 0.5}},
            ),
            (
                "search_research_organizations",
                {"query": "A&B", "country": "gb", "include_unlinked": True},
                "research_organizations",
                "&q=A%26B&country=gb&include_unlinked=true",
                {"id": "02jx3x895", "source": {"current": True}},
            ),
            (
                "list_research_accounts",
                {},
                "research_organizations/accounts",
                "",
                {"id": 1, "organization_matches": [{"relationship": "repository_owner"}]},
            ),
        ]
        for section in ("owners", "projects", "packages"):
            cases.append(
                (
                    "get_research_organization",
                    {"ror_id": "02jx3x895", "section": section, "include_descendants": True},
                    f"research_organizations/02jx3x895/{section}",
                    "&include_descendants=true",
                    {"id": 12},
                )
            )
        async with self.client() as client:
            for tool, args, path, filters, record in cases:
                with self.subTest(tool=tool, args=args):
                    first_path = f"/api/v1/{path}?page=1&per_page=1{filters}"
                    second_path = f"/api/v1/{path}?page=2&per_page=1{filters}"
                    self.api.responses[first_path] = (
                        200,
                        {"Link": '<https://untrusted.example/>; rel="next"'},
                        [record],
                    )
                    self.api.responses[second_path] = (200, {}, [])
                    first = await self.call(client, tool, {**args, "per_page": 1})
                    self.assertEqual([record], first["items"])
                    self.assertEqual("api", first["source"])
                    self.assertIn("retrieved_at", first)
                    self.assertEqual(2, first["next_page"])
                    second = await self.call(
                        client, tool, {**args, "per_page": 1, "page": first["next_page"]}
                    )
                    self.assertEqual([], second["items"])
                    self.assertIsNone(second["next_page"])
                    self.assertEqual([first_path, second_path], self.api.requests[-2:])

    async def test_record_tools_keep_source_status_and_identity(self):
        cases = [
            (
                "get_project_swhids",
                {"project_id": 12},
                "projects/12/swhids",
                {"project_id": 12, "status": "unchecked", "bytes_verified": False, "objects": []},
            ),
            (
                "get_package_version",
                {"package_id": 40, "version_id": 7},
                "packages/40/versions/7",
                {"id": 7, "package_id": 40, "number": "1.0", "metadata": {"revision": "abc"}},
            ),
            (
                "get_research_organization",
                {"ror_id": "02jx3x895"},
                "research_organizations/02jx3x895?include_descendants=false",
                {
                    "id": "02jx3x895",
                    "source": {"current": True, "version": "fixture"},
                    "coverage": {"relationship": "repository_owner"},
                },
            ),
        ]
        async with self.client() as client:
            for tool, args, path, record in cases:
                self.api.responses[f"/api/v1/{path}"] = (200, {}, record)
                data = await self.call(client, tool, args)
                self.assertEqual(record, data["record"])
                self.assertEqual("api", data["source"])

    async def test_bulk_lookups_send_json_and_preserve_unmatched_inputs(self):
        cases = [
            (
                "bulk_lookup_projects",
                "projects",
                "repository_urls",
                ["https://github.com/example/Stats", "https://github.com/missing/tool"],
                [
                    {
                        "input_url": "https://github.com/example/Stats",
                        "matches": [{"source": "repository_alias", "project": {"id": 12}}],
                    },
                    {"input_url": "https://github.com/missing/tool", "matches": []},
                ],
            ),
            (
                "bulk_lookup_packages",
                "packages",
                "purls",
                ["pkg:npm/%40example/stats@1", "pkg:pypi/missing"],
                [{"id": 40, "purl": "pkg:npm/%40example/stats"}],
            ),
        ]
        async with self.client() as client:
            for tool, resource, key, values, records in cases:
                path = f"/api/v1/{resource}/bulk_lookup"
                self.api.responses[path] = (200, {}, records)
                data = await self.call(client, tool, {key: values})
                self.assertEqual(values, data["inputs"])
                self.assertEqual(records, data["items"])
                self.assertEqual((path, {key: values}, "application/json"), self.api.posts[-1])

    async def test_new_tools_report_missing_routes_and_invalid_response_shapes(self):
        cases = [
            (
                "list_packages",
                {},
                "packages?page=1&per_page=10&ecosystem=&domain=&field=&sort=scientific_projects",
                {},
            ),
            (
                "list_package_versions",
                {"package_id": 40},
                "packages/40/versions?page=1&per_page=10",
                ["invalid"],
            ),
            (
                "get_project_external_identifiers",
                {"project_id": 12},
                "projects/12/external_identifiers?page=1&per_page=10",
                [{}] * 11,
            ),
            ("get_project_swhids", {"project_id": 12}, "projects/12/swhids", []),
            ("bulk_lookup_packages", {"purls": ["pkg:pypi/stats"]}, "packages/bulk_lookup", {}),
        ]
        async with self.client() as client:
            for tool, args, path, invalid in cases:
                for status, body, error in [
                    (404, {}, "not_found"),
                    (200, invalid, "invalid_response"),
                ]:
                    self.api.responses[f"/api/v1/{path}"] = (status, {}, body)
                    result = await client.call_tool(tool, args)
                    self.assertTrue(result.is_error)
                    self.assertEqual(error, result.structured_content["error"])

    async def test_new_tool_inputs_are_bounded_before_http_requests(self):
        cases = [
            ("get_project_external_identifiers", {"project_id": 1, "per_page": 26}),
            ("get_project_swhids", {"project_id": True}),
            ("bulk_lookup_projects", {"repository_urls": []}),
            ("bulk_lookup_packages", {"purls": ["pkg:pypi/stats"] * 26}),
            ("bulk_lookup_packages", {"purls": ["x" * 2001]}),
            ("list_packages", {"sort": "invalid"}),
            ("list_package_versions", {"package_id": 0}),
            ("get_package_version", {"package_id": 40, "version_id": "../other"}),
            ("get_package_repositories", {"package_id": 40, "page": 0}),
            ("search_research_organizations", {"query": "x" * 201}),
            ("search_research_organizations", {"country": "GBR"}),
            ("get_research_organization", {"ror_id": "../accounts"}),
            ("get_research_organization", {"ror_id": "02jx3x895", "section": "bad"}),
            ("list_research_accounts", {"per_page": 26}),
            ("list_package_versions", {"package_id": 40, "page": False}),
        ]
        async with self.client() as client:
            for tool, args in cases:
                with self.subTest(tool=tool):
                    result = await client.call_tool(tool, args)
                    self.assertTrue(result.is_error)
        self.assertEqual([], self.api.requests)

    async def test_extended_smoke_reports_errors_and_continues_independent_checks(self):
        path = "/api/v1/software/search?q=orbdot&limit=5&after_id=0"
        self.api.responses[path] = (200, {}, {"projects": [], "next_after_id": None})
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "scripts/smoke.py"),
            "--extended",
            cwd=str(ROOT),
            env={**os.environ, "SCIENCE_API_URL": self.api.url, "SCIENCE_SEEDS_DB": ""},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        self.assertEqual(1, process.returncode, stderr.decode())
        report = json.loads(stdout)
        self.assertIn("list_seed_projects", report["failed"])
        self.assertIn("list_packages", report["failed"])
        self.assertNotIn("search_software", report["failed"])
        self.assertEqual([], report["checks"]["search_software"]["projects"])
        self.assertIn("skipped", report["checks"]["package_details"])
        self.assertIn("skipped", report["checks"]["organization_details"])

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
