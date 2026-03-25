"""Path validation and output parsing for the latex-resume MCP server."""

import os
import re


def resolve_safe_path(file_path: str, workspace_dir: str) -> str:
    """Resolve file_path relative to workspace_dir and verify it stays inside.

    Raises ValueError if the path escapes the workspace, has a non-.tex
    extension, or does not exist.
    """
    if not file_path.endswith(".tex"):
        raise ValueError("Only .tex files are accepted")
    resolved = os.path.realpath(os.path.join(workspace_dir, file_path))
    if not resolved.startswith(workspace_dir + "/"):
        raise ValueError("Path escapes allowed directory")
    if not os.path.isfile(resolved):
        raise ValueError(f"File not found: {resolved}")
    return resolved


def parse_page_count(compiler_output: str) -> int | None:
    """Extract page count from pdflatex log output.

    Matches lines like: Output written on main.pdf (1 page, 52345 bytes).
    Returns None if the pattern is not found.
    """
    match = re.search(r"Output written on .+ \((\d+) pages?,", compiler_output)
    if match:
        return int(match.group(1))
    return None
