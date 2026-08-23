# Two stages: the build stage carries the toolchain, the runtime does not.
# Shipping compilers in the image is both weight and attack surface.
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
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

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY}"]
