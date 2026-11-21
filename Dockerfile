# mcpedia in a container: HTTP on port 8765, the vault mounted at /vault.
#
# Two images:
#   --target server        mcpedia only; forge and audit jobs can't run in it.
#   (default) server-with-claude  adds the Claude Code CLI, so jobs run inside the container.

# Optional local sources for the two downloads below. Both stages are empty, so by default the build downloads the
# model and the Claude Code CLI. To use local copies instead (e.g. behind a proxy that blocks the downloads), replace
# a stage with a folder of the same name: --build-context models=<folder with a filled model cache> or
# --build-context claude-binary=<folder holding a Linux `claude` binary>. See the README's "Restricted networks".
FROM scratch AS models
FROM scratch AS claude-binary

FROM python:3.14-slim AS server

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

# git for committing captures, ssh for pushing them to a remote such as a NAS, tzdata so captures
# get the local date (set TZ).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*' \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin mcpedia

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    WIKISMITH_CACHE_DIR=/cache \
    WIKISMITH_MODEL_DIR=/models \
    WIKISMITH_PORT=8765 \
    WIKISMITH_VAULT=/vault

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Bake the embedding model into the image, so the container needs no network to search. It lives in /models, not
# in the /cache volume: a volume keeps its first content, so a model changed later would never show up there. To use
# another model, set `search.model` in wikismith.yaml and build with --build-arg WIKISMITH_MODEL=<the same name>.
# A model already in the `models` build context isn't downloaded again. WIKISMITH_HF_MIRROR fetches it file by file
# from a Hugging Face mirror instead of huggingface.co.
ARG WIKISMITH_MODEL=BAAI/bge-small-en-v1.5
ARG WIKISMITH_HF_MIRROR=
RUN mkdir -p /cache /models /vault && chown mcpedia:mcpedia /cache /models /vault
COPY --from=models --chown=mcpedia:mcpedia . /models/
USER mcpedia
RUN wikismith fetch-model --model "$WIKISMITH_MODEL" ${WIKISMITH_HF_MIRROR:+--endpoint "$WIKISMITH_HF_MIRROR"}
ENV HF_HUB_OFFLINE=1

# The port comes from WIKISMITH_PORT, for `serve` and for the health check alike. /healthz answers 503 when the vault
# is unreadable or the search index failed to build, which makes the container unhealthy.
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"WIKISMITH_PORT\"]}/healthz', timeout=4)"]

ENTRYPOINT ["wikismith"]
CMD ["serve", "--transport", "http", "--host", "0.0.0.0"]


FROM server AS server-with-claude

# The Claude Code CLI for forge and audit jobs: a pinned release, checked against the
# release manifest's SHA-256. It authenticates with CLAUDE_CODE_OAUTH_TOKEN (from `claude
# setup-token`) at run time; nothing secret is in the image.
# A `claude` binary in the `claude-binary` build context is used instead of the download. It must be the Linux build
# for the image's architecture; set CLAUDE_CODE_SHA256 to check it (the download is always checked).
ARG CLAUDE_CODE_VERSION=2.1.318
ARG CLAUDE_CODE_SHA256=
USER root
RUN --mount=type=bind,from=claude-binary,target=/tmp/claude-binary python - <<EOF
import hashlib, json, os, platform, shutil, urllib.request
path = "/usr/local/bin/claude"
local = "/tmp/claude-binary/claude"
if os.path.isfile(local):
    shutil.copyfile(local, path)
    expected = "${CLAUDE_CODE_SHA256}".strip().lower()
    print("claude: using the binary from the claude-binary build context")
else:
    arch = {"x86_64": "x64", "aarch64": "arm64"}[platform.machine()]
    base = "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}"
    with urllib.request.urlopen(f"{base}/manifest.json") as r:
        expected = json.load(r)["platforms"][f"linux-{arch}"]["checksum"]
    urllib.request.urlretrieve(f"{base}/linux-{arch}/claude", path)
with open(path, "rb") as f:
    actual = hashlib.sha256(f.read()).hexdigest()
if expected and actual != expected:
    os.remove(path)
    raise SystemExit(f"claude checksum mismatch: {actual} != {expected}")
os.chmod(path, 0o755)
EOF
USER mcpedia
# Updates come with a new image, not from inside the container.
ENV DISABLE_AUTOUPDATER=1
RUN claude --version
