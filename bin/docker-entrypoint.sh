#!/usr/bin/env bash
# Migrations run at boot (spec D23): forward-only, and a failure is a
# *visible* non-zero exit — no silent fallback (D24).
set -euo pipefail

echo "[entrypoint] applying database migrations (alembic upgrade head)"
alembic upgrade head

echo "[entrypoint] migrations OK; exec: $*"
exec "$@"
