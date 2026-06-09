"""Validation helpers for the file-download MCP server.

All functions here are pure (no network beyond DNS resolution, no global state)
so they can be unit-tested directly. Security lives here: workspace path
confinement, protected-directory exclusion, URL scheme allow-listing, SSRF
blocking, and download size capping.
"""

import ipaddress
import os
import socket
from typing import Iterable
from urllib.parse import urlparse

# Directories an agent must never be able to write into via a download, because
# files placed there can become live without a shell (git hooks fire on commit;
# .claude*/ contents are auto-discovered as skills/subagents).
PROTECTED_DIRS = frozenset(
    {".git", ".claude", ".claude-skills", ".claude-agents"}
)

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that resolve to the host/other containers and must never be fetched.
BLOCKED_HOSTNAMES = frozenset({"host.docker.internal"})


def resolve_safe_dest(file_path: str, workspace_dir: str) -> str:
    """Resolve a download destination relative to the agent's workspace.

    Returns the absolute path to write to. Raises ValueError if the path is
    empty, escapes the workspace (traversal, absolute, or via symlink), targets
    a protected directory, or names an existing directory.
    """
    if not file_path or not file_path.strip():
        raise ValueError("Destination path is empty")

    workspace_dir = os.path.realpath(workspace_dir)
    resolved = os.path.realpath(os.path.join(workspace_dir, file_path))

    if resolved != workspace_dir and not resolved.startswith(workspace_dir + os.sep):
        raise ValueError("Destination escapes the workspace directory")

    rel = os.path.relpath(resolved, workspace_dir)
    components = rel.split(os.sep)
    if any(part in PROTECTED_DIRS for part in components):
        raise ValueError("Destination is inside a protected directory")

    if os.path.isdir(resolved):
        raise ValueError("Destination is an existing directory")

    return resolved


def validate_url(url: str) -> None:
    """Validate the URL scheme and that a host is present.

    Raises ValueError for non-http(s) schemes or a missing host. Does NOT do
    SSRF resolution — call assert_host_allowed on the resolved host for that.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"URL scheme must be http or https, got '{parsed.scheme}'")
    if not parsed.hostname:
        raise ValueError("URL has no host")


def is_blocked_ip(ip_str: str) -> bool:
    """True if the IP is loopback, private, link-local, reserved, multicast,
    or unspecified — i.e. anything that isn't a routable public address."""
    ip = ipaddress.ip_address(ip_str)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_host_allowed(host: str) -> None:
    """Resolve a hostname and raise ValueError if it maps to a blocked address.

    Blocks special Docker names by name, then resolves every A/AAAA record and
    blocks if any resolved address is non-public (defends against a public name
    that resolves to an internal IP).
    """
    if host.lower() in BLOCKED_HOSTNAMES:
        raise ValueError(f"Host '{host}' is blocked")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve host '{host}': {e}") from e

    for info in infos:
        ip_str = info[4][0]
        if is_blocked_ip(ip_str):
            raise ValueError(
                f"Host '{host}' resolves to a blocked address ({ip_str})"
            )


def read_capped(chunks: Iterable[bytes], max_bytes: int) -> bytes:
    """Concatenate byte chunks, raising ValueError if the total exceeds
    max_bytes. Used to stream a download with a hard size ceiling."""
    out = bytearray()
    for chunk in chunks:
        out.extend(chunk)
        if len(out) > max_bytes:
            raise ValueError(f"Download too large (exceeds {max_bytes} bytes)")
    return bytes(out)
