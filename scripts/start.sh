#!/usr/bin/env sh
# Render's start command.
#
# Migrations belong in a release phase that runs before new containers take
# traffic -- that is what scripts/release.sh is for, and it is what Render's
# preDeployCommand does on paid instance types. The free tier has no such hook,
# so they run here instead. That is only safe because the free plan runs a
# single instance: with more than one, every container would race the same DDL
# and the losers would crash-loop. Move this line to preDeployCommand in
# render.yaml before scaling past one instance.
set -eu

echo "==> alembic upgrade head"
alembic upgrade head

# exec, so uvicorn becomes PID 1 and receives SIGTERM directly. Without it the
# shell swallows the signal and the platform waits out its grace period before
# killing the container on every single deploy.
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-1}"
