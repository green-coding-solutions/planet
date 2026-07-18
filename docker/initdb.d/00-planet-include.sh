#!/usr/bin/env bash
# Point $PGDATA/postgresql.conf at the bind-mounted /etc/postgresql/planet.conf.
#
# Appended (not replaced) so the image's own generated settings survive, and
# placed last so planet.conf wins over them. postgresql.auto.conf is still read
# after all of this, which is what keeps ALTER SYSTEM --- and therefore the
# calibrated coefficients --- authoritative.
set -Eeuo pipefail

conf="$PGDATA/postgresql.conf"

if grep -q "^include_if_exists = '/etc/postgresql/planet.conf'" "$conf"; then
    exit 0
fi

cat >> "$conf" <<'EOF'

# ---- PLANET ----------------------------------------------------------------
# Server tuning for PLANET. Missing file = stock server.
include_if_exists = '/etc/postgresql/planet.conf'
EOF

echo "planet: postgresql.conf now includes /etc/postgresql/planet.conf"
