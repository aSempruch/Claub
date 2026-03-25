# LaTeX Resume MCP Server — Design Spec

## Purpose

Give the career agent a safe way to compile `.tex` files to PDF without granting shell access or the ability to escape its workspace sandbox.

## Tool

### `compile_latex(file_path: str) -> str`

Compiles a LaTeX file to PDF. Returns JSON with compilation result.

**Parameters:**
- `file_path` — Path to a `.tex` file, relative to the agent's workspace (`/claub/workspaces/{agent_name}/`)

**Behavior:**

1. Read `CLAUB_AGENT_NAME` from env. If unset, refuse to start (fail closed).
2. Validate `file_path` ends with `.tex`
3. Resolve path relative to `/claub/workspaces/{CLAUB_AGENT_NAME}/` using `os.path.realpath`
4. Reject if resolved path doesn't start with `ALLOWED_DIR + "/"` — blocks `../`, symlink escapes, etc.
5. Verify the `.tex` file exists
6. Run `pdflatex -interaction=nonstopmode -no-shell-escape <file>` in the directory containing the `.tex` file (so it finds sibling `.cls`/`.sty` files). Timeout: 30 seconds.
7. Verify the expected `.pdf` file was created (nonstopmode can exit 0 without producing output)
8. Parse page count from pdflatex log using regex: `Output written on .+ \((\d+) pages?\,`
9. If pages > 1, treat as failure
10. Return JSON result. Compiler output truncated to last 4000 characters.

**Return format (success):**
```json
{
  "success": true,
  "pdf_path": "/claub/workspaces/career/main.pdf",
  "pages": 1,
  "compiler_output": "<last 4000 chars of pdflatex stdout+stderr>"
}
```

**Return format (page overflow):**
```json
{
  "success": false,
  "error": "Resume is 2 pages (max 1)",
  "pages": 2,
  "pdf_path": "/claub/workspaces/career/main.pdf",
  "compiler_output": "<last 4000 chars of pdflatex stdout+stderr>"
}
```

**Return format (compilation failure):**
```json
{
  "success": false,
  "error": "<description>",
  "pages": null,
  "pdf_path": null,
  "compiler_output": "<last 4000 chars of pdflatex stdout+stderr>"
}
```

## Safety

- **Fail closed:** If `CLAUB_AGENT_NAME` is not set, the server refuses to start.
- **Path containment:** `os.path.realpath` resolves all symlinks, `../`, `./` before prefix check against `ALLOWED_DIR + "/"`. The trailing slash prevents prefix collisions (e.g., `/claub/workspaces/career-evil`).
- **Extension restriction:** Only `.tex` files accepted.
- **No shell injection:** `subprocess.run` with argument list, never `shell=True`.
- **No shell escape:** `pdflatex` runs with `-no-shell-escape`, blocking `\write18` arbitrary command execution.
- **Subprocess timeout:** 30 seconds, prevents infinite loops from malformed TeX macros.
- **Agent-scoped:** Allowed directory derived from `CLAUB_AGENT_NAME` env var, so each agent is confined to its own workspace.

## Transport and Wiring

**Transport:** stdio (same pattern as `leetcode-stats`)

**Server entry point:**
```python
if __name__ == "__main__":
    mcp.run(transport="stdio")
```

**Per-agent MCP config** (`/claub/config/agents/career.mcp.json`):
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

The `CLAUB_AGENT_NAME` env var is set by the bot in `claude_process.py` on the agent process. It propagates to stdio MCP subprocesses via standard child process env inheritance.

## Infrastructure

### New files
- `/claub/mcps/latex-resume/pyproject.toml` — Dependencies: `mcp[cli]`
- `/claub/mcps/latex-resume/server.py` — MCP server implementation

### Dockerfile changes
- Install `texlive-latex-base`, `texlive-latex-recommended`, and `texlive-fonts-recommended` via `apt-get`

### Agent wiring
- Create `/claub/config/agents/career.mcp.json` with the latex-resume MCP server config
- Add `mcp__latex-resume__*` to career agent's `allowed_tools_additional` in `agents.yaml`

## Page count detection

Parse pdflatex log output with regex `Output written on .+ \((\d+) pages?\,`. Handles both "1 page" and "N pages". Fallback: if the pattern isn't found, report pages as unknown and succeed (don't block on a parsing failure).
