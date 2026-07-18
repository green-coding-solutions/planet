/* planet 0.1.0 -> 0.2.0: wall-clock capture + awake-power term. */
\echo Use "ALTER EXTENSION planet UPDATE" to load this file. \quit

CREATE FUNCTION planet_last_wall_seconds() RETURNS float8
    AS 'MODULE_PATHNAME', 'planet_last_wall_seconds'  LANGUAGE C;
CREATE FUNCTION planet_last_awake_g()       RETURNS float8
    AS 'MODULE_PATHNAME', 'planet_last_awake_g'       LANGUAGE C;

DROP FUNCTION planet_last();
CREATE FUNCTION planet_last(
    OUT carbon_g     float8,
    OUT compute_g    float8,
    OUT io_g         float8,
    OUT wal_g        float8,
    OUT awake_g      float8,
    OUT energy_j     float8,
    OUT cpu_seconds  float8,
    OUT wall_seconds float8,
    OUT blks_read    bigint,
    OUT blks_written bigint,
    OUT wal_bytes    bigint
) RETURNS record
LANGUAGE sql AS $$
    SELECT planet_last_carbon_g(),
           planet_last_compute_g(),
           planet_last_io_g(),
           planet_last_wal_g(),
           planet_last_awake_g(),
           planet_last_energy_joules(),
           planet_last_cpu_seconds(),
           planet_last_wall_seconds(),
           planet_last_blks_read(),
           planet_last_blks_written(),
           planet_last_wal_bytes();
$$;
COMMENT ON FUNCTION planet_last() IS 'Footprint (flow) of the last top-level query.';
