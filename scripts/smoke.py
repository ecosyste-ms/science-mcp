import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "science_mcp.server"],
        env={key: value for key, value in os.environ.items() if key.startswith("SCIENCE_")},
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=30) as client:
            await client.initialize()
            tools = await client.list_tools()
            page = await client.call_tool("list_seed_projects", {"per_page": 1})
            if page.is_error:
                raise RuntimeError(page.content)
            project_id = page.structured_content["projects"][0]["project_id"]
            context = await client.call_tool("get_project_context", {"project_id": project_id})
            if context.is_error:
                raise RuntimeError(context.content)
            summary = {
                "tools": [tool.name for tool in tools.tools],
                "context": context.structured_content,
            }
            if os.environ.get("SCIENCE_SEEDS_DB"):
                lookup = await client.call_tool("lookup_software", {"query": "orbdot", "limit": 5})
                if lookup.is_error:
                    raise RuntimeError(lookup.content)
                summary["lookup"] = lookup.structured_content
            print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
