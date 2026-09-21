import json
import re
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .errors import ScienceError

DEFAULT_URL = "https://science.ecosyste.ms/api/v1"
MAX_BODY_BYTES = 10 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ScienceAPI:
    def __init__(self, base_url: str = DEFAULT_URL):
        try:
            uri = urlsplit(base_url)
            valid = (
                uri.scheme in {"http", "https"}
                and uri.hostname
                and not uri.username
                and not uri.password
                and not uri.query
                and not uri.fragment
                and not any(char.isspace() for char in base_url)
                and (uri.port is None or uri.port > 0)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ScienceError("configuration_error", "SCIENCE_API_URL must be an HTTP(S) base URL")
        self.base_url = base_url.rstrip("/")

    def get(self, path: str, **query) -> tuple[object, object, str]:
        url = f"{self.base_url}/{path}"
        if query:
            url += "?" + urlencode(query)
        request = Request(
            url, headers={"Accept": "application/json", "User-Agent": f"science-mcp/{__version__}"}
        )
        try:
            with build_opener(NoRedirect).open(request, timeout=15) as response:
                body = response.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise ScienceError("response_too_large", "Science response exceeds 10 MiB")
                return json.loads(body), response.headers, url
        except HTTPError as error:
            message = f"Science returned HTTP {error.code}"
            if error.code == 429 and error.headers.get("Retry-After"):
                message += f"; retry after {error.headers['Retry-After']}"
            code = {404: "not_found", 429: "rate_limited"}.get(error.code, "api_error")
            error.close()
            raise ScienceError(code, message) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ScienceError("invalid_response", "Science returned invalid JSON") from error
        except (URLError, TimeoutError, OSError) as error:
            raise ScienceError(
                "connection_error", "Could not read the Science API response"
            ) from error

    def seeds(self, page: int, per_page: int) -> dict:
        data, headers, url = self.get("projects/search_seeds", page=page, per_page=per_page)
        if not isinstance(data, list) or not all(
            isinstance(item, dict) and type(item.get("project_id")) is int for item in data
        ):
            raise ScienceError("invalid_response", "Science returned an invalid seed page")
        return {
            "source": "api",
            "url": url,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "page": page,
            "per_page": per_page,
            "next_page": page + 1
            if re.search(r';\s*rel="next"', headers.get("Link", ""))
            else None,
            "projects": data,
        }

    def context(self, project_id: int, section: str, offset: int, limit: int) -> dict:
        data, _, url = self.get(f"projects/{project_id}/search_context")
        if (
            not isinstance(data, dict)
            or data.get("project_id") != project_id
            or not all(
                isinstance(data.get(key), list)
                for key in ("descriptions", "languages", "packages", "direct_dependencies")
            )
        ):
            raise ScienceError("invalid_response", "Science returned invalid project context")
        result = {
            "source": "api",
            "url": url,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "project_id": project_id,
            "repository_url": data.get("repository_url"),
            "updated_at": data.get("updated_at"),
            "last_synced_at": data.get("last_synced_at"),
            "dependencies_indexed_at": data.get("dependencies_indexed_at"),
            "section": section,
        }
        if section == "summary":
            result.update(
                descriptions=data["descriptions"],
                languages=data["languages"],
                package_count=len(data["packages"]),
                direct_dependency_count=len(data["direct_dependencies"]),
            )
        else:
            records = data[section]
            items = records[offset : offset + limit]
            result.update(
                items=items,
                total=len(records),
                offset=offset,
                next_offset=offset + len(items) if offset + len(items) < len(records) else None,
            )
        return result
