# PLANET PostgreSQL extension (PGXS build).  Requires PostgreSQL 18.
#
#   make            # build planet.so against the pg_config on PATH
#   make install    # install into the cluster's lib/extension dirs
#   make installcheck
#
# Use a specific server:  make PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config
# Skip LLVM bitcode (nothing here for the JIT to inline):  make with_llvm=no

MODULES    = planet
EXTENSION  = planet
DATA       = planet--0.1.0.sql planet--0.1.0--0.2.0.sql planet--0.2.0.sql
PGFILEDESC = "planet - per-query carbon accounting"

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
