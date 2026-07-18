#!/usr/bin/env bash
# Install the planet extension.
#
# POSTGRES_DB was cloned from template1 before this script runs, so installing
# into template1 alone would not reach it --- both are done explicitly. Creating
# the extension only writes catalog rows; planet.so is dlopen'd lazily, so this
# works even though the bootstrap server has no session_preload_libraries yet.
set -Eeuo pipefail

for db in template1 "${POSTGRES_DB:-postgres}"; do
    psql -v ON_ERROR_STOP=1 --no-password \
         --username "${POSTGRES_USER:-postgres}" --dbname "$db" \
         -c 'CREATE EXTENSION IF NOT EXISTS planet;'
    echo "planet: extension installed in ${db}"
done
