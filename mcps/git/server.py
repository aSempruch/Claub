"""MCP server exposing local git operations scoped to an agent's workspace.

Sidesteps Claude Code's hardcoded `cd`/`git -C` "bare repository attack"
heuristics by exposing git as MCP tool calls instead of bash commands.

All operations are scoped to /claub/workspaces/{agent}. Network and code-exec
escape hatches are blocked at the git layer by /etc/gitconfig
(hooksPath=/dev/null, protocol.allow=never) — this server only adds a second
layer of subcommand filtering and path containment.
"""

import json
import os
import subprocess

from mcp.server.fastmcp import FastMCP

AGENT_NAME = os.environ.get("CLAUB_AGENT_NAME")
if not AGENT_NAME:
    raise RuntimeError("CLAUB_AGENT_NAME environment variable is required")

WORKSPACE_DIR = f"/claub/workspaces/{AGENT_NAME}"
GIT_TIMEOUT = 30
MAX_OUTPUT_CHARS = 8000

mcp = FastMCP("git")


def _resolve_repo(repo_path: str) -> str:
    """Resolve repo_path relative to the agent workspace and verify containment."""
    if not repo_path:
        repo_path = "."
    resolved = os.path.realpath(os.path.join(WORKSPACE_DIR, repo_path))
    if resolved != WORKSPACE_DIR and not resolved.startswith(WORKSPACE_DIR + "/"):
        raise ValueError(f"Path escapes workspace: {repo_path}")
    if not os.path.isdir(resolved):
        raise ValueError(f"Directory not found: {resolved}")
    return resolved


def _run(repo: str, args: list[str]) -> dict:
    """Run `git -C repo <args>` and return a result dict."""
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"git {' '.join(args)} timed out", "stdout": "", "stderr": ""}
    output = (result.stdout or "")[-MAX_OUTPUT_CHARS:]
    err = (result.stderr or "")[-MAX_OUTPUT_CHARS:]
    return {
        "success": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": output,
        "stderr": err,
    }


def _call(repo_path: str, args: list[str]) -> str:
    try:
        repo = _resolve_repo(repo_path)
    except ValueError as e:
        return json.dumps({"success": False, "error": str(e)})
    return json.dumps(_run(repo, args))


@mcp.tool()
def init(repo_path: str = ".") -> str:
    """Initialize a new git repo at repo_path (relative to workspace)."""
    return _call(repo_path, ["init"])


@mcp.tool()
def status(repo_path: str = ".") -> str:
    """Show working tree status (porcelain + branch)."""
    return _call(repo_path, ["status"])


@mcp.tool()
def add(repo_path: str, paths: list[str]) -> str:
    """Stage one or more paths. Use ['.'] to stage everything."""
    if not paths:
        return json.dumps({"success": False, "error": "paths must be non-empty"})
    return _call(repo_path, ["add", "--", *paths])


@mcp.tool()
def commit(repo_path: str, message: str) -> str:
    """Create a commit with the given message. Stages nothing — call add first."""
    if not message.strip():
        return json.dumps({"success": False, "error": "message must be non-empty"})
    return _call(repo_path, ["commit", "-m", message])


@mcp.tool()
def log(repo_path: str = ".", limit: int = 20) -> str:
    """Show commit history (oneline format)."""
    return _call(repo_path, ["log", f"-n{int(limit)}", "--oneline", "--decorate"])


@mcp.tool()
def diff(repo_path: str = ".", staged: bool = False, path: str | None = None) -> str:
    """Show diff. Set staged=True for staged changes; optional path to limit scope."""
    args = ["diff"]
    if staged:
        args.append("--staged")
    if path:
        args.extend(["--", path])
    return _call(repo_path, args)


@mcp.tool()
def show(repo_path: str = ".", ref: str = "HEAD", path: str | None = None) -> str:
    """Show a commit (default HEAD), or a single file at that ref when path is given."""
    target = f"{ref}:{path}" if path else ref
    return _call(repo_path, ["show", target])


@mcp.tool()
def branch(repo_path: str = ".", list_all: bool = True) -> str:
    """List branches."""
    args = ["branch"]
    if list_all:
        args.append("-a")
    return _call(repo_path, args)


@mcp.tool()
def checkout(repo_path: str, ref: str, create: bool = False) -> str:
    """Checkout a branch or commit. Set create=True to create a new branch."""
    args = ["checkout"]
    if create:
        args.append("-b")
    args.append(ref)
    return _call(repo_path, args)


@mcp.tool()
def reset(repo_path: str, ref: str = "HEAD", hard: bool = False) -> str:
    """Reset to ref. Set hard=True for --hard (discards working tree changes)."""
    args = ["reset"]
    if hard:
        args.append("--hard")
    args.append(ref)
    return _call(repo_path, args)


@mcp.tool()
def restore(repo_path: str, paths: list[str], staged: bool = False) -> str:
    """Restore files from index or HEAD. staged=True unstages."""
    if not paths:
        return json.dumps({"success": False, "error": "paths must be non-empty"})
    args = ["restore"]
    if staged:
        args.append("--staged")
    args.extend(["--", *paths])
    return _call(repo_path, args)


@mcp.tool()
def rm(repo_path: str, paths: list[str]) -> str:
    """Remove files from the working tree and index."""
    if not paths:
        return json.dumps({"success": False, "error": "paths must be non-empty"})
    return _call(repo_path, ["rm", "--", *paths])


@mcp.tool()
def mv(repo_path: str, source: str, dest: str) -> str:
    """Move/rename a file."""
    return _call(repo_path, ["mv", source, dest])


@mcp.tool()
def tag(repo_path: str = ".", name: str | None = None, ref: str | None = None, message: str | None = None) -> str:
    """Create or list tags. Omit name to list all tags. Provide message for an annotated tag."""
    if not name:
        return _call(repo_path, ["tag", "--list"])
    if not name.strip():
        return json.dumps({"success": False, "error": "tag name must be non-empty"})
    args = ["tag"]
    if message:
        args.extend(["-a", name, "-m", message])
    else:
        args.append(name)
    if ref:
        args.append(ref)
    return _call(repo_path, args)


@mcp.tool()
def tag_delete(repo_path: str, name: str) -> str:
    """Delete a local tag."""
    if not name.strip():
        return json.dumps({"success": False, "error": "tag name must be non-empty"})
    return _call(repo_path, ["tag", "-d", name])


if __name__ == "__main__":
    mcp.run(transport="stdio")
