"""MCP server for compiling LaTeX resumes to PDF."""

import json
import logging
import os
import subprocess

from mcp.server.fastmcp import FastMCP

from helpers import parse_page_count, resolve_safe_path

logger = logging.getLogger(__name__)

AGENT_NAME = os.environ.get("CLAUB_AGENT_NAME")
if not AGENT_NAME:
    raise RuntimeError(
        "CLAUB_AGENT_NAME environment variable is required but not set"
    )

WORKSPACE_DIR = f"/claub/workspaces/{AGENT_NAME}"
COMPILE_TIMEOUT = 30
MAX_OUTPUT_CHARS = 4000

mcp = FastMCP("latex-resume")


@mcp.tool()
def compile_latex(file_path: str) -> str:
    """Compile a LaTeX file to PDF.

    Args:
        file_path: Path to a .tex file, relative to the agent's workspace directory.

    Returns:
        JSON with success status, pdf_path, page count, and compiler output.
        Fails if the resume exceeds 1 page.
    """
    try:
        resolved = resolve_safe_path(file_path, WORKSPACE_DIR)
    except ValueError as e:
        return json.dumps({"success": False, "error": str(e), "pages": None, "pdf_path": None, "compiler_output": ""})

    tex_dir = os.path.dirname(resolved)
    tex_filename = os.path.basename(resolved)
    pdf_path = resolved.rsplit(".", 1)[0] + ".pdf"

    try:
        result = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-no-shell-escape", tex_filename],
            cwd=tex_dir,
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT,
        )
        full_output = result.stdout + result.stderr
        pages = parse_page_count(full_output)
        compiler_output = full_output[-MAX_OUTPUT_CHARS:]
    except subprocess.TimeoutExpired:
        return json.dumps({"success": False, "error": "Compilation timed out (30s)", "pages": None, "pdf_path": None, "compiler_output": ""})

    if not os.path.isfile(pdf_path):
        return json.dumps({"success": False, "error": "Compilation failed — no PDF produced", "pages": None, "pdf_path": None, "compiler_output": compiler_output})

    if pages is not None and pages > 1:
        return json.dumps({"success": False, "error": f"Resume is {pages} pages (max 1)", "pages": pages, "pdf_path": pdf_path, "compiler_output": compiler_output})

    return json.dumps({"success": True, "pdf_path": pdf_path, "pages": pages, "compiler_output": compiler_output})


if __name__ == "__main__":
    mcp.run(transport="stdio")
