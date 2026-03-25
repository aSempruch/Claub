# LaTeX Resume MCP Server — Design Spec

## Purpose

Give the career agent a safe way to compile `.tex` files to PDF without granting shell access or the ability to escape its workspace sandbox.

## Tool

### `compile_latex(file_path: str) -> str`

Compiles a LaTeX file to PDF. Returns JSON with compilation result.

**Parameters:**
- `file_path` — Path to a `.tex` file, relative to the agent's workspace (`/claub/workspaces/{agent_name}/`)

**Behavior:**

1. Validate `file_path` ends with `.tex`
2. Resolve path relative to `/claub/workspaces/{CLAUB_AGENT_NAME}/` using `os.path.realpath`
3. Reject if resolved path doesn't start with `ALLOWED_DIR + "/"`— blocks `../`, symlink escapes, etc.
4. Verify the `.tex` file exists
5. Run `pdflatex -interaction=nonstopmode -no-shell-escape <file>` in the directory containing the `.tex` file (so it finds sibling `.cls`/`.sty` files)
6. Parse page count from the generated PDF or log
7. If pages > 1, treat as failure
8. Return JSON result

**Return format (success):**
```json
{
  "success": true,
  "pdf_path": "/claub/workspaces/career/main.pdf",
  "pages": 1,
  "compiler_output": "<pdflatex stdout+stderr>"
}
```

**Return format (page overflow):**
```json
{
  "success": false,
  "error": "Resume is 2 pages (max 1)",
  "pages": 2,
  "pdf_path": "/claub/workspaces/career/main.pdf",
  "compiler_output": "<pdflatex stdout+stderr>"
}
```

**Return format (compilation failure):**
```json
{
  "success": false,
  "error": "<description>",
  "pages": null,
  "pdf_path": null,
  "compiler_output": "<pdflatex stdout+stderr>"
}
```

## Safety

- **Path containment:** `os.path.realpath` resolves all symlinks, `../`, `./` before prefix check against `ALLOWED_DIR + "/"`. The trailing slash prevents prefix collisions (e.g., `/claub/workspaces/career-evil`).
- **Extension restriction:** Only `.tex` files accepted.
- **No shell injection:** `subprocess.run` with argument list, never `shell=True`.
- **No shell escape:** `pdflatex` runs with `-no-shell-escape`, blocking `\write18` arbitrary command execution.
- **Agent-scoped:** Allowed directory derived from `CLAUB_AGENT_NAME` env var, so each agent is confined to its own workspace.

## Infrastructure

### New files
- `/claub/mcps/latex-resume/pyproject.toml` — Dependencies: `mcp[cli]`
- `/claub/mcps/latex-resume/server.py` — MCP server implementation

### Dockerfile changes
- Install `texlive-latex-base` and `texlive-latex-recommended` via `apt-get`

### Agent wiring
- Create or update `/claub/config/agents/career.mcp.json` to add the `latex-resume` MCP server
- Add `mcp__latex-resume__*` to career agent's `allowed_tools_additional` in `agents.yaml`

## Page count detection

Use `pdflatex` log output — it writes lines like `Output written on main.pdf (1 page, 12345 bytes)`. Parse the page count from this line. Fallback: if the log line isn't found, report pages as unknown and succeed (don't block on a parsing failure).
