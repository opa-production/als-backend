# Two stages: the build stage carries the toolchain, the runtime does not.
# Shipping compilers in the image is both weight and attack surface.
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# `app/` is copied alongside the metadata because pyproject declares
# `packages = ["app"]`: setuptools resolves that at build time and fails with
# "package directory 'app' does not exist" if only pyproject.toml is present.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --prefix=/install .


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:$PATH"

WORKDIR /app

COPY --from=build /install /usr/local
COPY . .

# Non-root. A container that runs as root is one file-write bug away from
# being someone else's shell.
RUN useradd --system --uid 10001 als && chown -R als:als /app
USER als

EXPOSE 8000

# Workers are set from the environment so the same image scales up without a
# rebuild. Two per core is the usual starting point for an IO-bound service.
ENV WEB_CONCURRENCY=2

# ${PORT} so the image also runs unchanged on a platform that assigns the port
# (Render, Cloud Run, Fly); falls back to the exposed 8000 for compose.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY}"]
