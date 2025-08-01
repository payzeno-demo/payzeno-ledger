# payzeno-ledger — production image.
#
# Two stages. The builder resolves the lock into a venv; the runtime copies the venv
# and the source. Nothing else. No uv, no compiler, no git in the shipped image.
#
# Built by payzeno-infrastructure's docker-compose.yml (build.context: ../payzeno-ledger)
# and by .github/workflows/ci.yml on every push to main.

# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.2.11 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/payzeno/.venv

# payzeno-contracts lives on the internal index. CI injects the credential as a
# build secret; it must never end up in a layer.
ARG UV_INDEX_PAYZENO_INTERNAL_USERNAME=ci
ENV UV_INDEX_PAYZENO_INTERNAL_USERNAME=${UV_INDEX_PAYZENO_INTERNAL_USERNAME}

WORKDIR /opt/payzeno

# Dependency layer first — pyproject + lock only, so a source edit does not
# re-resolve 60 packages on every build.
COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=payzeno_index_password \
    UV_INDEX_PAYZENO_INTERNAL_PASSWORD="$(cat /run/secrets/payzeno_index_password 2>/dev/null || echo '')" \
    uv sync --locked --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

RUN --mount=type=secret,id=payzeno_index_password \
    UV_INDEX_PAYZENO_INTERNAL_PASSWORD="$(cat /run/secrets/payzeno_index_password 2>/dev/null || echo '')" \
    uv sync --locked --no-dev

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# curl is here for exactly one reason: the compose and ECS healthchecks hit
# /healthz with it. Do not add anything else to this list without asking infra.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 payzeno \
 && useradd --uid 10001 --gid payzeno --home-dir /opt/payzeno --no-create-home payzeno

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/payzeno/.venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/payzeno/.venv

WORKDIR /opt/payzeno

COPY --from=builder --chown=payzeno:payzeno /opt/payzeno /opt/payzeno

USER payzeno
EXPOSE 8000

# The container runs the API. Workers and consumers are the SAME image with a
# different command, set by the task definition:
#   web       -> uvicorn app.main:create_app --factory
#   worker    -> python -m app.workers
#   consumer  -> python -m app.consumers
#   migrate   -> alembic upgrade head   (a one-shot task, before the web rollout)
# Keep it that way. One image, four commands, one sha to roll back.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
