# LaTeX Resume MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a stdio MCP server that lets agents compile `.tex` files to PDF, sandboxed to their own workspace directory.

**Architecture:** Single-tool MCP server (`compile_latex`) that validates paths against the agent's workspace, runs `pdflatex` via subprocess, and returns JSON with the PDF path, page count, and compiler output. Wired to the career agent via per-agent MCP config.

**Tech Stack:** Python, FastMCP (`mcp[cli]`), `pdflatex` (texlive), pytest

**Spec:** `docs/superpowers/specs/2026-03-24-latex-resume-mcp-design.md`

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `mcps/latex-resume/helpers.py` | Path validation, page count parsing (testable, no env guard) |
| Create | `mcps/latex-resume/server.py` | MCP server — imports helpers, subprocess, response formatting |
| Create | `mcps/latex-resume/pyproject.toml` | Dependencies (`mcp[cli]`) |
| Create | `bot/tests/test_latex_resume_mcp.py` | Unit tests for helpers + mocked compile_latex tests |
| Modify | `Dockerfile:4-9` | Add texlive packages to apt-get install |
| Create | `config/agents/career.mcp.json` (in ~/docker/claub/) | Wire MCP to career agent |
| Modify | `config/agents.yaml` (in ~/docker/claub/) | Add `mcp__latex-resume__*` to career's `allowed_tools_additional` |

> **Note:** `mcps/latex-resume/` lives in the project repo and is bind-mounted into the container at `/claub/mcps/latex-resume/`. Config files under `~/docker/claub/` are instance config (not in the repo).

---

### Task 1: MCP Server — Helpers and Server

**Files:**
- Create: `mcps/latex-resume/pyproject.toml`
- Create: `mcps/latex-resume/helpers.py`
- Create: `mcps/latex-resume/server.py`

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "latex-resume"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "mcp[cli]",
]
```

- [ ] **Step 2: Create helpers.py — path validation and page parsing**

Pure functions with no module-level side effects. Takes `workspace_dir` as a parameter so tests can inject a temp directory.

```python
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
```

- [ ] **Step 3: Create server.py — MCP tool that uses helpers**

```python
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
        compiler_output = (result.stdout + result.stderr)[-MAX_OUTPUT_CHARS:]
    except subprocess.TimeoutExpired:
        return json.dumps({"success": False, "error": "Compilation timed out (30s)", "pages": None, "pdf_path": None, "compiler_output": ""})

    if not os.path.isfile(pdf_path):
        return json.dumps({"success": False, "error": "Compilation failed — no PDF produced", "pages": None, "pdf_path": None, "compiler_output": compiler_output})

    pages = parse_page_count(compiler_output)

    if pages is not None and pages > 1:
        return json.dumps({"success": False, "error": f"Resume is {pages} pages (max 1)", "pages": pages, "pdf_path": pdf_path, "compiler_output": compiler_output})

    return json.dumps({"success": True, "pdf_path": pdf_path, "pages": pages, "compiler_output": compiler_output})


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

- [ ] **Step 4: Commit**

```bash
git add mcps/latex-resume/pyproject.toml mcps/latex-resume/helpers.py mcps/latex-resume/server.py
git commit -m "feat: add latex-resume MCP server"
```

---

### Task 2: Unit Tests

**Files:**
- Create: `bot/tests/test_latex_resume_mcp.py`

Tests import `helpers.py` directly (no module-level env guard, no MCP transport needed). `compile_latex` tests mock `subprocess.run`.

- [ ] **Step 1: Write tests for resolve_safe_path**

```python
"""Tests for latex-resume MCP server."""

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add helpers to path (helpers.py has no module-level side effects)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "mcps", "latex-resume"))

from helpers import parse_page_count, resolve_safe_path


class TestResolveSafePath:
    def test_valid_relative_path(self, tmp_path):
        workspace = str(tmp_path)
        tex_file = tmp_path / "main.tex"
        tex_file.write_text("\\documentclass{article}")
        assert resolve_safe_path("main.tex", workspace) == str(tex_file)

    def test_subdirectory_path(self, tmp_path):
        workspace = str(tmp_path)
        subdir = tmp_path / "versions"
        subdir.mkdir()
        tex_file = subdir / "v2.tex"
        tex_file.write_text("\\documentclass{article}")
        assert resolve_safe_path("versions/v2.tex", workspace) == str(tex_file)

    def test_rejects_non_tex_extension(self, tmp_path):
        with pytest.raises(ValueError, match="Only .tex files"):
            resolve_safe_path("main.pdf", str(tmp_path))

    def test_rejects_path_traversal(self, tmp_path):
        with pytest.raises(ValueError, match="escapes allowed directory"):
            resolve_safe_path("../../etc/passwd.tex", str(tmp_path))

    def test_rejects_absolute_escape(self, tmp_path):
        with pytest.raises(ValueError, match="escapes allowed directory"):
            resolve_safe_path("/etc/passwd.tex", str(tmp_path))

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(ValueError, match="File not found"):
            resolve_safe_path("nonexistent.tex", str(tmp_path))

    def test_rejects_symlink_escape(self, tmp_path):
        """A symlink inside the workspace pointing outside must be rejected."""
        workspace = str(tmp_path)
        outside = tmp_path.parent / "outside.tex"
        outside.write_text("\\documentclass{article}")
        link = tmp_path / "escape.tex"
        link.symlink_to(outside)
        with pytest.raises(ValueError, match="escapes allowed directory"):
            resolve_safe_path("escape.tex", workspace)
```

- [ ] **Step 2: Write tests for parse_page_count**

```python
class TestParsePageCount:
    def test_single_page(self):
        assert parse_page_count("Output written on main.pdf (1 page, 52345 bytes).") == 1

    def test_multiple_pages(self):
        assert parse_page_count("Output written on main.pdf (3 pages, 152345 bytes).") == 3

    def test_no_match_returns_none(self):
        assert parse_page_count("some random output") is None
```

- [ ] **Step 3: Write tests for compile_latex (mocked subprocess)**

```python
class TestCompileLatex:
    """Test compile_latex with mocked subprocess — covers timeout, missing PDF, page overflow."""

    @pytest.fixture(autouse=True)
    def _setup_workspace(self, tmp_path, monkeypatch):
        self.workspace = str(tmp_path)
        self.tex_file = tmp_path / "main.tex"
        self.tex_file.write_text("\\documentclass{article}")
        self.pdf_file = tmp_path / "main.pdf"
        # Import server with patched env and workspace
        monkeypatch.setenv("CLAUB_AGENT_NAME", "test-agent")
        if "server" in sys.modules:
            del sys.modules["server"]
        import server
        server.WORKSPACE_DIR = self.workspace
        self.compile_latex = server.compile_latex

    @patch("server.subprocess.run")
    def test_success(self, mock_run):
        self.pdf_file.write_text("fake pdf")
        mock_run.return_value = MagicMock(
            stdout="Output written on main.pdf (1 page, 52345 bytes).",
            stderr="",
        )
        result = json.loads(self.compile_latex("main.tex"))
        assert result["success"] is True
        assert result["pages"] == 1
        assert result["pdf_path"] == str(self.pdf_file)

    @patch("server.subprocess.run")
    def test_page_overflow(self, mock_run):
        self.pdf_file.write_text("fake pdf")
        mock_run.return_value = MagicMock(
            stdout="Output written on main.pdf (2 pages, 102345 bytes).",
            stderr="",
        )
        result = json.loads(self.compile_latex("main.tex"))
        assert result["success"] is False
        assert "2 pages" in result["error"]
        assert result["pages"] == 2

    @patch("server.subprocess.run")
    def test_no_pdf_produced(self, mock_run):
        # Don't create the PDF file
        mock_run.return_value = MagicMock(stdout="! Missing $ inserted.", stderr="")
        result = json.loads(self.compile_latex("main.tex"))
        assert result["success"] is False
        assert "no PDF produced" in result["error"]

    @patch("server.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pdflatex", timeout=30))
    def test_timeout(self, mock_run):
        result = json.loads(self.compile_latex("main.tex"))
        assert result["success"] is False
        assert "timed out" in result["error"]

    def test_path_validation_error(self):
        result = json.loads(self.compile_latex("../../etc/passwd.tex"))
        assert result["success"] is False
        assert "escapes" in result["error"]
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/you/Claude && uv run --directory bot --extra dev pytest bot/tests/test_latex_resume_mcp.py -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add bot/tests/test_latex_resume_mcp.py
git commit -m "test: add unit tests for latex-resume MCP"
```

---

### Task 3: Dockerfile — Add texlive

**Files:**
- Modify: `Dockerfile:4-9`

- [ ] **Step 1: Add texlive packages to apt-get install**

In the existing `apt-get install` block in the Dockerfile, add `texlive-latex-base`, `texlive-latex-recommended`, and `texlive-fonts-recommended`:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    texlive-latex-base texlive-latex-recommended texlive-fonts-recommended && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: Commit**

```bash
git add Dockerfile
git commit -m "feat: add texlive to Docker image for LaTeX compilation"
```

---

### Task 4: Agent Wiring — MCP Config and Permissions

**Files:**
- Create: `~/docker/claub/config/agents/career.mcp.json`
- Modify: `~/docker/claub/config/agents.yaml`

- [ ] **Step 1: Create career.mcp.json**

At `~/docker/claub/config/agents/career.mcp.json`:

```json
{
  "mcpServers": {
    "latex-resume": {
      "command": "uv",
      "args": ["--directory", "/claub/mcps/latex-resume", "run", "server.py"]
    }
  }
}
```

- [ ] **Step 2: Add mcp__latex-resume__* to career agent's allowed_tools_additional**

In `~/docker/claub/config/agents.yaml`, update the career agent entry:

```yaml
  career:
    channel_id: "1485393270598140066"
    display_name: "Career Advisor"
    allowed_tools_additional:
      - "mcp__leetcode-stats__*"
      - "mcp__latex-resume__*"
```

- [ ] **Step 3: Note — instance config, not tracked in repo**

These files live in `~/docker/claub/` which is instance config. No git commit needed.

---

### Task 5: Integration Test — Build and Compile

- [ ] **Step 1: Rebuild the Docker image**

```bash
cd /Users/you/Claude && docker compose up -d --build
```

This installs texlive, runs `uv sync` on the new MCP server, and picks up the career.mcp.json config.

- [ ] **Step 2: Test pdflatex is available in the container**

```bash
docker exec claude-claub-1 pdflatex --version
```

Expected: Version info from pdfTeX.

- [ ] **Step 3: Test compilation via the career agent in Discord**

Send a message to the career agent asking it to compile the resume. Verify it calls `compile_latex("main.tex")` and produces `main.pdf`.
