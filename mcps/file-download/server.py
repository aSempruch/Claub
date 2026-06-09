"""MCP server for downloading files from the web into an agent's workspace.

Least-privilege alternative to giving the agent a shell with curl: it can only
fetch an http(s) URL and write the bytes to a validated path inside its own
workspace. It cannot read local files, run anything, reach internal services
(SSRF-blocked), or write into git/.claude dirs. Size-capped to prevent disk
exhaustion. Follows redirects but re-validates the host on every hop.
"""

import json
import logging
import os
import sys

import httpx

from helpers import (
    assert_host_allowed,
    read_capped,
    resolve_safe_dest,
    validate_url,
)

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

AGENT_NAME = os.environ.get("CLAUB_AGENT_NAME")
if not AGENT_NAME:
    raise RuntimeError(
        "CLAUB_AGENT_NAME environment variable is required but not set"
    )

WORKSPACE_DIR = f"/claub/workspaces/{AGENT_NAME}"
REQUEST_TIMEOUT = 30
MAX_REDIRECTS = 5
HARD_MAX_MB = 100
DEFAULT_MAX_MB = 50

mcp = FastMCP("file-download")


def _err(message: str) -> str:
    return json.dumps(
        {"success": False, "error": message, "path": None, "bytes": None}
    )


@mcp.tool()
def download_file(url: str, dest_path: str, max_mb: int = DEFAULT_MAX_MB) -> str:
    """Download a file from an http(s) URL into your workspace.

    Args:
        url: The http:// or https:// URL to download.
        dest_path: Destination path relative to your workspace directory
            (e.g. "downloads/photo.jpg"). Parent folders are created as needed.
            Cannot escape the workspace or target .git/.claude directories.
        max_mb: Maximum download size in megabytes (default 50, hard cap 100).

    Returns:
        JSON with success status, the absolute path written, byte count, and the
        server-reported content_type. On failure, success is false with an error.
    """
    try:
        validate_url(url)
        resolved = resolve_safe_dest(dest_path, WORKSPACE_DIR)
    except ValueError as e:
        return _err(str(e))

    max_bytes = min(max(int(max_mb), 1), HARD_MAX_MB) * 1024 * 1024

    try:
        with httpx.Client(follow_redirects=False, timeout=REQUEST_TIMEOUT) as client:
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                validate_url(current)
                assert_host_allowed(httpx.URL(current).host)
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return _err("Redirect response without a location header")
                        current = str(resp.url.join(location))
                        continue
                    resp.raise_for_status()
                    content = read_capped(resp.iter_bytes(), max_bytes)
                    content_type = resp.headers.get("content-type", "")
                    break
            else:
                return _err(f"Too many redirects (>{MAX_REDIRECTS})")
    except ValueError as e:
        return _err(str(e))
    except httpx.HTTPStatusError as e:
        return _err(f"HTTP {e.response.status_code} from server")
    except httpx.HTTPError as e:
        return _err(f"Download failed: {e}")

    parent = os.path.dirname(resolved)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(resolved, "wb") as f:
        f.write(content)

    return json.dumps(
        {
            "success": True,
            "path": resolved,
            "bytes": len(content),
            "content_type": content_type,
        }
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
