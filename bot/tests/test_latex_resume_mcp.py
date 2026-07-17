"""Tests for latex-resume MCP server."""

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# Load helpers.py under a unique module name (helpers.py has no module-level
# side effects). Several MCPs have a helpers.py, so a bare `import helpers`
# resolves to whichever MCP's test imported first — load by file path instead.
# MCP servers live in ~/docker/claub/mcps/ (instance config, bind-mounted into container)
import importlib.util

_LATEX_RESUME_DIR = os.path.expanduser("~/docker/claub/mcps/latex-resume")
_spec = importlib.util.spec_from_file_location(
    "latex_resume_helpers", os.path.join(_LATEX_RESUME_DIR, "helpers.py")
)
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
parse_page_count = _helpers.parse_page_count
resolve_safe_path = _helpers.resolve_safe_path


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


class TestParsePageCount:
    def test_single_page(self):
        assert parse_page_count("Output written on main.pdf (1 page, 52345 bytes).") == 1

    def test_multiple_pages(self):
        assert parse_page_count("Output written on main.pdf (3 pages, 152345 bytes).") == 3

    def test_no_match_returns_none(self):
        assert parse_page_count("some random output") is None


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
        # server.py does `from helpers import ...`, and another MCP's test may
        # have cached a different bare `helpers`/`server` — clear both and
        # import with the latex-resume dir on sys.path only for this import.
        sys.modules.pop("server", None)
        sys.modules.pop("helpers", None)
        sys.path.insert(0, _LATEX_RESUME_DIR)
        try:
            import server
        finally:
            sys.path.remove(_LATEX_RESUME_DIR)
            sys.modules.pop("helpers", None)
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

    @patch("server.subprocess.run")
    def test_artifacts_cleaned_up_on_success(self, mock_run):
        self.pdf_file.write_text("fake pdf")
        for ext in (".aux", ".log", ".out", ".toc"):
            (self.tex_file.parent / f"main{ext}").write_text("junk")
        mock_run.return_value = MagicMock(
            stdout="Output written on main.pdf (1 page, 52345 bytes).",
            stderr="",
        )
        result = json.loads(self.compile_latex("main.tex"))
        assert result["success"] is True
        for ext in (".aux", ".log", ".out", ".toc"):
            assert not (self.tex_file.parent / f"main{ext}").exists()
        assert self.pdf_file.exists()

    @patch("server.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pdflatex", timeout=30))
    def test_artifacts_cleaned_up_on_timeout(self, mock_run):
        (self.tex_file.parent / "main.aux").write_text("junk")
        result = json.loads(self.compile_latex("main.tex"))
        assert result["success"] is False
        assert not (self.tex_file.parent / "main.aux").exists()
