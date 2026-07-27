# claub-exec — agent execution sandbox image

Throwaway container image for `mcp__sandbox__run` / `install`. Built by hand,
never in `docker-compose.yml` — only ever `docker run --rm` from the exec bridge.

## Build

    docker build -t claub-exec docker/exec-sandbox/

Rebuild after editing the Dockerfile or bumping a pin. On aarch64 the first
build compiles `pycairo`, `manimpango`, and `moderngl` from source (no wheels
exist) — expect a few minutes. The build toolchain stays in the final image
because `install` also needs a compiler at runtime for sdist-only packages.

Result is roughly 2.2 GB.

## Smoke test

    docker run --rm claub-exec python -c "import manim; print(manim.__version__)"   # 0.20.1

A full render under the real sandbox flags (read-only root, tmpfs, no network),
which is what the bridge actually issues:

    SM=/Users/you/.claub-exec-smoke; mkdir -p $SM/.claude
    printf 'from manim import Scene, Text, Write\nclass T(Scene):\n    def construct(self):\n        self.play(Write(Text("ok")))\n' > $SM/t.py
    docker run --rm --network none --read-only \
      --tmpfs /tmp:size=256m,exec --cap-drop ALL --security-opt no-new-privileges \
      --memory 1g --cpus 1.5 --pids-limit 256 \
      -e HOME=/tmp -e MPLCONFIGDIR=/tmp/mpl \
      -v $SM:/claub/workspaces/smoke -w /claub/workspaces/smoke \
      claub-exec bash -c "manim -ql t.py T && ls media/videos/t/480p15/T.mp4"

**The mount source must be a host path Colima shares into the VM** — that is
`/Users/you` (rw) and `/tmp/playwright` (ro). A `-v /tmp/...` source silently
mounts an *empty* directory instead of failing, which looks like "my file
disappeared". The real `workspaces_root` lives under `/Users/you`, so this
only bites ad-hoc testing.

## Pins

- `manim==0.20.1` (phase-2 example scenes target this API)
- `uv` 0.11.32
- No LaTeX (MathTex/Tex excluded to save ~1 GB — the skill uses Text/MarkupText)
