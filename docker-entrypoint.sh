#!/bin/sh
# Container entrypoint.
#
# Migrations and seeding run before the workers start, and only when asked, so
# that scaling to several replicas does not have every one of them racing to
# migrate the same database. Run them from a single job (or set the flags on
# one replica only) in a clustered deployment.
set -eu

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    echo "==> Applying database migrations"
    flask db upgrade
fi

if [ "${SEED_LIBRARY:-1}" = "1" ]; then
    echo "==> Loading the shipped clause library"
    flask seed-library
fi

# Report PDF capability at boot rather than at the first failed download.
if flask check-pdf >/dev/null 2>&1; then
    echo "==> PDF rendering: available"
else
    echo "==> PDF rendering: UNAVAILABLE (WeasyPrint native libraries missing)"
fi

echo "==> Starting: $*"
exec "$@"
