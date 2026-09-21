import json
from collections.abc import Callable

from mcp.types import CallToolResult, TextContent

from .errors import ScienceError

MAX_DATA_BYTES = 64 * 1024
MAX_TEXT_LENGTH = 2000
MAX_ARRAY_LENGTH = 25


def tool_response(operation: Callable[[], dict]) -> CallToolResult:
    truncations = []

    def bound(value, path="$", depth=0):
        if depth > 12:
            truncations.append({"path": path, "reason": "nesting_limit"})
            return None
        if isinstance(value, dict):
            return {key: bound(item, f"{path}.{key}", depth + 1) for key, item in value.items()}
        if isinstance(value, list):
            if len(value) > MAX_ARRAY_LENGTH:
                truncations.append(
                    {
                        "path": path,
                        "original_count": len(value),
                        "returned_count": MAX_ARRAY_LENGTH,
                    }
                )
            return [
                bound(item, f"{path}[{index}]", depth + 1)
                for index, item in enumerate(value[:MAX_ARRAY_LENGTH])
            ]
        if isinstance(value, str) and len(value) > MAX_TEXT_LENGTH:
            truncations.append(
                {
                    "path": path,
                    "original_length": len(value),
                    "returned_length": MAX_TEXT_LENGTH,
                }
            )
            return value[:MAX_TEXT_LENGTH]
        return value

    try:
        data = bound(operation())
        data["truncations"] = truncations
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_DATA_BYTES:
            raise ScienceError(
                "result_too_large",
                "Reduce per_page or limit, or request a narrower context section",
            )
        return CallToolResult(
            content=[TextContent(type="text", text=text)], structured_content=data
        )
    except ScienceError as error:
        data = {"error": error.code, "message": str(error)}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(data))],
            structured_content=data,
            is_error=True,
        )
