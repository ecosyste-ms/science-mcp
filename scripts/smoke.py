import argparse
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check_tools(client, extended):
    report = {"tools": [tool.name for tool in (await client.list_tools()).tools], "checks": {}}
    failed = []

    async def call(name, arguments, label=None):
        key = label or name
        result = await client.call_tool(name, arguments)
        report["checks"][key] = result.structured_content
        if result.is_error:
            failed.append(key)
            return {}
        return result.structured_content

    seeds = await call("list_seed_projects", {"per_page": 1})
    if seeds.get("projects"):
        project = seeds["projects"][0]
        project_id = project["project_id"]
        await call("get_project_context", {"project_id": project_id})
        if extended:
            await call(
                "get_project_external_identifiers", {"project_id": project_id, "per_page": 1}
            )
            await call("get_project_swhids", {"project_id": project_id})
            await call("bulk_lookup_projects", {"repository_urls": [project["repository_url"]]})
    else:
        report["checks"]["project_details"] = {"skipped": "No seed projects returned"}
    await call("lookup_software", {"query": "orbdot", "limit": 5})
    await call("search_software", {"query": "orbdot", "limit": 5})
    if extended:
        packages = await call("bulk_lookup_packages", {"purls": ["pkg:pypi/numpy"]})
        if packages.get("items"):
            package_id = packages["items"][0]["id"]
            await call("get_package_repositories", {"package_id": package_id, "per_page": 1})
            versions = await call(
                "list_package_versions", {"package_id": package_id, "per_page": 1}
            )
            if versions.get("items"):
                await call(
                    "get_package_version",
                    {
                        "package_id": package_id,
                        "version_id": versions["items"][0]["id"],
                    },
                )
            else:
                report["checks"]["get_package_version"] = {"skipped": "No stored versions returned"}
        else:
            report["checks"]["package_details"] = {"skipped": "No NumPy package returned"}
        organizations = await call("search_research_organizations", {"per_page": 1})
        await call("list_research_accounts", {"per_page": 1})
        if organizations.get("items"):
            ror_id = organizations["items"][0]["id"]
            for section in ("summary", "owners", "projects", "packages"):
                await call(
                    "get_research_organization",
                    {
                        "ror_id": ror_id,
                        "section": section,
                        "per_page": 1,
                    },
                    label=f"organization_{section}",
                )
        else:
            report["checks"]["organization_details"] = {
                "skipped": "No linked institutions returned"
            }
        await call("list_packages", {"per_page": 1})
    report["failed"] = failed
    return report


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--extended", action="store_true")
    options = parser.parse_args()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "science_mcp.server"],
        env={key: value for key, value in os.environ.items() if key.startswith("SCIENCE_")},
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=30) as client:
            await client.initialize()
            report = await check_tools(client, options.extended)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
