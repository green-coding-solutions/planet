/* planet--0.2.0.sql --- PLANET SQL interface (adds wall_seconds + awake) */

-- complain if script is sourced in psql, rather than via CREATE EXTENSION
\echo Use "CREATE EXTENSION planet" to load this file. \quit

/* ---- scalar getters backed by C (last top-level query) ------------------ */
CREATE FUNCTION planet_last_carbon_g()      RETURNS float8
    AS 'MODULE_PATHNAME', 'planet_last_carbon_g'      LANGUAGE C;
CREATE FUNCTION planet_last_energy_joules() RETURNS float8
    AS 'MODULE_PATHNAME', 'planet_last_energy_joules' LANGUAGE C;
CREATE FUNCTION planet_last_cpu_seconds()   RETURNS float8
    AS 'MODULE_PATHNAME', 'planet_last_cpu_seconds'   LANGUAGE C;
CREATE FUNCTION planet_last_wall_seconds() RETURNS float8
    AS 'MODULE_PATHNAME', 'planet_last_wall_seconds'  LANGUAGE C;
CREATE FUNCTION planet_last_awake_g()       RETURNS float8
    AS 'MODULE_PATHNAME', 'planet_last_awake_g'       LANGUAGE C;
CREATE FUNCTION planet_last_blks_read()     RETURNS bigint
    AS 'MODULE_PATHNAME', 'planet_last_blks_read'     LANGUAGE C;
CREATE FUNCTION planet_last_blks_written()  RETURNS bigint
    AS 'MODULE_PATHNAME', 'planet_last_blks_written'  LANGUAGE C;
CREATE FUNCTION planet_last_wal_bytes()     RETURNS bigint
    AS 'MODULE_PATHNAME', 'planet_last_wal_bytes'     LANGUAGE C;
CREATE FUNCTION planet_last_compute_g()     RETURNS float8
    AS 'MODULE_PATHNAME', 'planet_last_compute_g'     LANGUAGE C;
CREATE FUNCTION planet_last_io_g()          RETURNS float8
    AS 'MODULE_PATHNAME', 'planet_last_io_g'          LANGUAGE C;
CREATE FUNCTION planet_last_wal_g()         RETURNS float8
    AS 'MODULE_PATHNAME', 'planet_last_wal_g'         LANGUAGE C;
CREATE FUNCTION planet_reset()              RETURNS void
    AS 'MODULE_PATHNAME', 'planet_reset'              LANGUAGE C;

/* ---- convenience: the last query's footprint as one row ----------------- */
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

/*
 * planet_table_carbon(rel, age_seconds)
 *
 * The stored STOCK of a table (embodied share + retention accrued over
 * age_seconds), per Eq. 1.  This is deliberately separate from the per-query
 * FLOW above: "reads meter, writes mint" --- reading a table does not change
 * this number; only its bytes and its age do.  Pass NULL age for the embodied
 * component only.  Coefficients come from the planet.* GUCs.
 */
CREATE FUNCTION planet_table_carbon(rel regclass, age_seconds float8 DEFAULT NULL)
RETURNS TABLE (
    bytes        bigint,
    embodied_g   float8,
    retention_g  float8,
    stock_g      float8
)
LANGUAGE sql STABLE AS $$
    WITH p AS (
        SELECT pg_total_relation_size(rel)                                    AS bytes,
               current_setting('planet.embodied_gco2_per_byte', true)::float8 AS emb_pb,
               current_setting('planet.idle_watts_per_byte',    true)::float8 AS idle_pb,
               current_setting('planet.grid_gco2_per_kwh',      true)::float8 AS grid
    )
    SELECT bytes,
           COALESCE(emb_pb, 1.5e-8) * bytes                                    AS embodied_g,
           CASE WHEN age_seconds IS NULL THEN 0
                ELSE COALESCE(idle_pb, 2.5e-12) * bytes * age_seconds
                     / 3.6e6 * COALESCE(grid, 400.0)
           END                                                                 AS retention_g,
           COALESCE(emb_pb, 1.5e-8) * bytes
             + CASE WHEN age_seconds IS NULL THEN 0
                    ELSE COALESCE(idle_pb, 2.5e-12) * bytes * age_seconds
                         / 3.6e6 * COALESCE(grid, 400.0)
               END                                                             AS stock_g
    FROM p;
$$;

COMMENT ON FUNCTION planet_last()                       IS 'Footprint (flow) of the last top-level query.';
COMMENT ON FUNCTION planet_table_carbon(regclass, float8) IS 'Stored stock (embodied + retention) of a table.';
