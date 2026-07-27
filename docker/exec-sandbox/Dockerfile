# Throwaway execution sandbox for Claub agents. Built by hand (see README),
# never referenced by docker-compose.yml — only ever `docker run --rm`.
#
# The build toolchain is MANDATORY on aarch64, not optional polish: pycairo and
# manimpango publish NO Linux wheels, and moderngl publishes none for aarch64,
# so all three compile from source both at bake time AND whenever `install`
# pulls an sdist-only package at runtime. It stays in the final image.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        build-essential \
        pkg-config \
        libcairo2-dev \
        libpango1.0-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# uv, pinned (bump deliberately, never floating).
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

# Bake the common scientific/animation stack into the system interpreter so a
# workspace venv created with --system-site-packages sees them for free.
# manim pinned so phase-2 example scenes target a known ManimCE API.
RUN uv pip install --system --break-system-packages \
        manim==0.20.1 \
        numpy \
        matplotlib \
        networkx \
        pillow

# No LaTeX: MathTex/Tex need ~1 GB of texlive. The skill uses Text/MarkupText.
# No default CMD — the bridge always passes `bash -c "<command>"`.
