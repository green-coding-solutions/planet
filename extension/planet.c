/*-------------------------------------------------------------------------
 *
 * planet.c
 *      PLANET --- per-query carbon accounting inside PostgreSQL.
 *
 * PLANET installs the standard executor hooks, snapshots the counters the
 * engine already maintains (BufferUsage, WalUsage, per-backend CPU time via
 * getrusage), and at ExecutorEnd turns their per-query delta into an energy
 * and carbon estimate using a calibrated linear model.
 *
 * Design invariant --- "reads meter, writes mint": a query REPORTS the
 * marginal flow it caused; it does not mint stored stock.  Storage stock
 * (embodied + retention) is a property of the data and is exposed separately
 * via planet_table_carbon() in the SQL wrapper.
 *
 * No hardware energy counter (RAPL) is read at run time.  RAPL is used only
 * offline, to fit the coefficients that live in the planet.* GUCs.
 *
 * Requires PostgreSQL 18.  A native EXPLAIN (PLANET) option can be registered
 * via RegisterExtensionExplainOption() (see README / TODO).
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#if PG_VERSION_NUM < 180000
#error "PLANET requires PostgreSQL 18 or later"
#endif

#include <sys/resource.h>

#include "access/parallel.h"
#include "executor/executor.h"
#include "executor/instrument.h"
#include "fmgr.h"
#include "miscadmin.h"
#include "port/atomics.h"
#include "portability/instr_time.h"
#include "storage/dsm_registry.h"
#include "storage/proc.h"
#include "utils/guc.h"

PG_MODULE_MAGIC;

/* ---- saved hook entry points --------------------------------------------- */
static ExecutorStart_hook_type  prev_ExecutorStart  = NULL;
static ExecutorRun_hook_type    prev_ExecutorRun    = NULL;
static ExecutorFinish_hook_type prev_ExecutorFinish = NULL;
static ExecutorEnd_hook_type    prev_ExecutorEnd    = NULL;

/* Only top-level statements are accounted (nested queries are folded into
 * their caller).  This mirrors auto_explain / pg_stat_statements. */
static int nesting_level = 0;

/* ---- GUCs (coefficients; fit once, offline --- see code/calibration) ------ */
static bool   planet_enabled            = true;
static bool   planet_report             = false;
static double planet_cpu_active_watts   = 15.0;    /* dynamic W per busy core */
/* Power the package draws for the DURATION of a query merely because it is
 * awake (out of deep C-states), above idle and independent of how busy it is.
 * Charged per wall-clock second.  Empirically ~4 W on a Xeon E-2176G: fitting
 * the calibration sweep against a wall meter without this term leaves 22.8%
 * held-out error; with it, 2.0%.  Default 0 = the pre-0.2 model. */
static double planet_awake_watts        = 0.0;
static double planet_joules_per_read    = 8e-5;    /* J per block read        */
static double planet_joules_per_write   = 1.6e-4;  /* J per block written     */
static double planet_joules_per_wal_byte= 2e-8;    /* J per WAL byte          */
static double planet_grid_gco2_per_kwh  = 400.0;   /* grid carbon intensity   */
/* Storage-stock coefficients: read from SQL (planet_table_carbon), defined
 * here so the reserved planet.* prefix accepts them and users can tune them. */
static double planet_embodied_gco2_per_byte = 1.5e-8;  /* ~15 gCO2e/GB (SSD)  */
static double planet_idle_watts_per_byte    = 2.5e-12; /* ~5 W over a 2 TB dev*/

/* ---- per-backend state --------------------------------------------------- */
typedef struct PlanetState
{
    /* snapshot taken at the start of the current top-level query */
    bool        capturing;
    BufferUsage buf0;
    WalUsage    wal0;
    double      cpu0;           /* seconds */
    instr_time  wall0;
    int         slot;           /* worker-CPU slot claimed by this leader, or -1 */

    /* result of the last completed top-level query */
    bool        valid;
    double      cpu_seconds;    /* leader + parallel workers */
    double      wall_seconds;
    int64       blks_read;
    int64       blks_written;
    uint64      wal_bytes;
    double      energy_j;       /* total joules */
    double      carbon_g;       /* total gCO2e  */
    double      cg_compute;     /* gCO2e, compute component */
    double      cg_awake;       /* gCO2e, awake-time component */
    double      cg_io;          /* gCO2e, block-I/O component */
    double      cg_wal;         /* gCO2e, WAL component */
} PlanetState;

static PlanetState st;

/* ---- worker-CPU aggregation ----------------------------------------------
 *
 * getrusage(RUSAGE_SELF) is per process, so a parallel worker's CPU time is
 * invisible to the leader.  PostgreSQL aggregates the workers' BufferUsage /
 * WalUsage into the leader, but not their CPU.  Each worker therefore
 * publishes its own getrusage delta at ExecutorEnd into a small shared array,
 * keyed by the leader's pid, and the leader folds it in at its own
 * ExecutorEnd.  Workers finish before the leader's ExecutorEnd runs (Gather
 * shuts them down while the plan is being run), so no wait is needed.
 *
 * The segment comes from the DSM registry (PG17+), which exists precisely so
 * a session-loaded module can share memory without shared_preload_libraries.
 * Slots are claimed lock-free: a leader CASes its pid into a free slot at
 * ExecutorStart and zeroes it at ExecutorEnd; workers locate the slot by pid
 * scan and add microseconds atomically.  If all slots are busy (more than
 * PLANET_MAX_LEADERS concurrent parallel leaders), workers find no slot and
 * that query's worker CPU is silently uncounted -- the pre-0.2 behaviour.
 */
#define PLANET_MAX_LEADERS 128

typedef struct PlanetWorkerSlot
{
    pg_atomic_uint32 pid;       /* leader pid, 0 = free */
    pg_atomic_uint64 cpu_usec;  /* accumulated worker CPU, microseconds */
} PlanetWorkerSlot;

typedef struct PlanetShmem
{
    PlanetWorkerSlot slots[PLANET_MAX_LEADERS];
} PlanetShmem;

static PlanetShmem *planet_shm = NULL;

static void
planet_shmem_init(void *ptr)
{
    PlanetShmem *shm = (PlanetShmem *) ptr;
    int         i;

    for (i = 0; i < PLANET_MAX_LEADERS; i++)
    {
        pg_atomic_init_u32(&shm->slots[i].pid, 0);
        pg_atomic_init_u64(&shm->slots[i].cpu_usec, 0);
    }
}

static void
planet_shmem_attach(void)
{
    bool        found;

    if (planet_shm == NULL)
        planet_shm = GetNamedDSMSegment("planet_worker_cpu",
                                        sizeof(PlanetShmem),
                                        planet_shmem_init, &found);
}

static int
planet_slot_claim(void)
{
    int         i;

    planet_shmem_attach();
    for (i = 0; i < PLANET_MAX_LEADERS; i++)
    {
        uint32      expected = 0;

        if (pg_atomic_compare_exchange_u32(&planet_shm->slots[i].pid,
                                           &expected, (uint32) MyProcPid))
        {
            pg_atomic_write_u64(&planet_shm->slots[i].cpu_usec, 0);
            return i;
        }
    }
    return -1;
}

static uint64
planet_slot_release(int slot)
{
    uint64      usec;

    if (slot < 0 || planet_shm == NULL)
        return 0;
    usec = pg_atomic_read_u64(&planet_shm->slots[slot].cpu_usec);
    pg_atomic_write_u32(&planet_shm->slots[slot].pid, 0);
    return usec;
}

/* In a worker: credit our CPU delta to the leader's slot, if it has one. */
static void
planet_worker_publish(double cpu_seconds)
{
    PGPROC     *leader = MyProc->lockGroupLeader;
    uint32      leader_pid;
    int         i;

    if (leader == NULL || cpu_seconds <= 0)
        return;
    leader_pid = (uint32) leader->pid;

    planet_shmem_attach();
    for (i = 0; i < PLANET_MAX_LEADERS; i++)
    {
        if (pg_atomic_read_u32(&planet_shm->slots[i].pid) == leader_pid)
        {
            pg_atomic_fetch_add_u64(&planet_shm->slots[i].cpu_usec,
                                    (uint64) (cpu_seconds * 1e6));
            return;
        }
    }
}

void _PG_init(void);
void _PG_fini(void);

/* ---- helpers ------------------------------------------------------------- */

/*
 * Per-backend CPU seconds (user+sys) via getrusage.
 *
 * KNOWN LIMITATION: RUSAGE_SELF is the *leader* backend only.  Parallel worker
 * CPU is therefore NOT counted here, so this UNDER-estimates the compute of
 * parallel plans.  (Block I/O and WAL are fine: PostgreSQL aggregates worker
 * BufferUsage/WalUsage into the leader.)  It matters most when comparing a
 * parallel plan against a serial one: the parallel plan should show HIGHER
 * total core-seconds, and here it does not.  Two fixes, in order of rigor:
 *   1. aggregate per-worker rusage at parallel-worker exit (cf. pg_stat_kcache)
 *      -- the correct in-engine fix; TODO.
 *   2. cross-check against RAPL package energy or a wall meter, which capture
 *      all cores -- see ../calibration.
 */
static double
cpu_seconds_now(void)
{
    struct rusage r;

    getrusage(RUSAGE_SELF, &r);
    return (r.ru_utime.tv_sec + r.ru_utime.tv_usec / 1e6) +
           (r.ru_stime.tv_sec + r.ru_stime.tv_usec / 1e6);
}

static inline double
joules_to_gco2(double joules)
{
    /* J -> kWh (/3.6e6) -> gCO2e (* grid intensity) */
    return (joules / 3.6e6) * planet_grid_gco2_per_kwh;
}

/* ---- executor hooks ------------------------------------------------------ */

static void
planet_ExecutorStart(QueryDesc *queryDesc, int eflags)
{
    if (prev_ExecutorStart)
        prev_ExecutorStart(queryDesc, eflags);
    else
        standard_ExecutorStart(queryDesc, eflags);

    if (planet_enabled && nesting_level == 0)
    {
        /* Snapshot the global counters *after* standard start-up. */
        st.buf0 = pgBufferUsage;
        st.wal0 = pgWalUsage;
        st.cpu0 = cpu_seconds_now();
        INSTR_TIME_SET_CURRENT(st.wall0);
        /* Parallel leaders park a slot for their workers' CPU time.  Workers
         * never claim one: they publish into their leader's. */
        st.slot = IsParallelWorker() ? -1 : planet_slot_claim();
        st.capturing = true;
    }
}

static void
planet_ExecutorRun(QueryDesc *queryDesc, ScanDirection direction, uint64 count)
{
    nesting_level++;
    PG_TRY();
    {
        if (prev_ExecutorRun)
            prev_ExecutorRun(queryDesc, direction, count);
        else
            standard_ExecutorRun(queryDesc, direction, count);
    }
    PG_FINALLY();
    {
        nesting_level--;
    }
    PG_END_TRY();
}

static void
planet_ExecutorFinish(QueryDesc *queryDesc)
{
    nesting_level++;
    PG_TRY();
    {
        if (prev_ExecutorFinish)
            prev_ExecutorFinish(queryDesc);
        else
            standard_ExecutorFinish(queryDesc);
    }
    PG_FINALLY();
    {
        nesting_level--;
    }
    PG_END_TRY();
}

static void
planet_ExecutorEnd(QueryDesc *queryDesc)
{
    if (planet_enabled && nesting_level == 0 && st.capturing)
    {
        int64   reads;
        int64   writes;
        uint64  wal_bytes;
        double  cpu;
        double  wall;
        double  io_j;
        double  wal_j;
        double  compute_j;
        double  awake_j;
        instr_time wall1;

        cpu = cpu_seconds_now() - st.cpu0;
        if (cpu < 0)
            cpu = 0;

        /*
         * A parallel worker runs these hooks in its own backend.  Its block
         * and WAL counters are aggregated into the leader by the executor
         * itself; its CPU time is not.  Publish the CPU delta to the leader's
         * slot and stop -- no report, no planet_last() state: the leader's
         * numbers are the query's numbers.
         */
        if (IsParallelWorker())
        {
            planet_worker_publish(cpu);
            st.capturing = false;
            goto chain;
        }

        /* Fold in whatever our workers published (0 if no slot / no workers). */
        cpu += planet_slot_release(st.slot) / 1e6;
        st.slot = -1;

        /* Per-query counter deltas (reads = shared+local+temp). */
        reads = (pgBufferUsage.shared_blks_read - st.buf0.shared_blks_read)
              + (pgBufferUsage.local_blks_read  - st.buf0.local_blks_read)
              + (pgBufferUsage.temp_blks_read   - st.buf0.temp_blks_read);
        writes = (pgBufferUsage.shared_blks_written - st.buf0.shared_blks_written)
               + (pgBufferUsage.local_blks_written  - st.buf0.local_blks_written)
               + (pgBufferUsage.temp_blks_written   - st.buf0.temp_blks_written);
        wal_bytes = pgWalUsage.wal_bytes - st.wal0.wal_bytes;

        INSTR_TIME_SET_CURRENT(wall1);
        INSTR_TIME_SUBTRACT(wall1, st.wall0);
        wall = INSTR_TIME_GET_DOUBLE(wall1);

        /* Energy model (Eq. 2 + awake term): the package being out of deep
         * C-states costs planet.awake_watts for the query's whole duration,
         * on top of the busy-time compute term. */
        compute_j = cpu * planet_cpu_active_watts;
        awake_j   = wall * planet_awake_watts;
        io_j      = reads  * planet_joules_per_read
                  + writes * planet_joules_per_write;
        wal_j     = (double) wal_bytes * planet_joules_per_wal_byte;

        st.cpu_seconds  = cpu;
        st.wall_seconds = wall;
        st.blks_read    = reads;
        st.blks_written = writes;
        st.wal_bytes    = wal_bytes;
        st.energy_j     = compute_j + awake_j + io_j + wal_j;
        st.cg_compute   = joules_to_gco2(compute_j);
        st.cg_awake     = joules_to_gco2(awake_j);
        st.cg_io        = joules_to_gco2(io_j);
        st.cg_wal       = joules_to_gco2(wal_j);
        st.carbon_g     = st.cg_compute + st.cg_awake + st.cg_io + st.cg_wal;
        st.valid        = true;
        st.capturing    = false;

        /*
         * %.6g, not %.4f.  A per-query footprint is routinely ~1e-4 gCO2e, which
         * %.4f rounds to a single significant digit: two plans differing by 30%
         * in carbon both print "0.0002".  Anything parsing this line then reads
         * the quantisation, not the measurement.  %g keeps six significant
         * digits wherever the exponent lands.  Scripts that want full float8
         * precision should call planet_last() instead of scraping this.
         */
        if (planet_report)
            ereport(INFO,
                    (errmsg("PLANET carbon=%.6g gCO2e "
                            "(compute=%.6g awake=%.6g io=%.6g wal=%.6g) "
                            "cpu=%.6gs wall=%.6gs blks_read=" INT64_FORMAT
                            " blks_written=" INT64_FORMAT
                            " wal=" UINT64_FORMAT "B",
                            st.carbon_g, st.cg_compute, st.cg_awake,
                            st.cg_io, st.cg_wal,
                            st.cpu_seconds, st.wall_seconds,
                            st.blks_read, st.blks_written,
                            st.wal_bytes)));
    }

chain:

    if (prev_ExecutorEnd)
        prev_ExecutorEnd(queryDesc);
    else
        standard_ExecutorEnd(queryDesc);
}

/* ---- module load / unload ------------------------------------------------ */

void
_PG_init(void)
{
    DefineCustomBoolVariable("planet.enabled",
                             "Enable PLANET per-query carbon accounting.",
                             NULL, &planet_enabled, true,
                             PGC_SUSET, 0, NULL, NULL, NULL);

    DefineCustomBoolVariable("planet.report",
                             "Emit an INFO line with the carbon of each query.",
                             NULL, &planet_report, false,
                             PGC_USERSET, 0, NULL, NULL, NULL);

    DefineCustomRealVariable("planet.cpu_active_watts",
                             "Dynamic power per busy core (W); calibrated.",
                             NULL, &planet_cpu_active_watts, 15.0,
                             0.0, 1000.0,
                             PGC_SUSET, 0, NULL, NULL, NULL);

    DefineCustomRealVariable("planet.awake_watts",
                             "Package power while a query runs, above idle, "
                             "independent of load (W); calibrated.",
                             NULL, &planet_awake_watts, 0.0,
                             0.0, 1000.0,
                             PGC_SUSET, 0, NULL, NULL, NULL);

    DefineCustomRealVariable("planet.joules_per_read",
                             "Energy per block read (J); calibrated.",
                             NULL, &planet_joules_per_read, 8e-5,
                             0.0, 1.0,
                             PGC_SUSET, 0, NULL, NULL, NULL);

    DefineCustomRealVariable("planet.joules_per_write",
                             "Energy per block written (J); calibrated.",
                             NULL, &planet_joules_per_write, 1.6e-4,
                             0.0, 1.0,
                             PGC_SUSET, 0, NULL, NULL, NULL);

    DefineCustomRealVariable("planet.joules_per_wal_byte",
                             "Energy per WAL byte (J); calibrated.",
                             NULL, &planet_joules_per_wal_byte, 2e-8,
                             0.0, 1.0,
                             PGC_SUSET, 0, NULL, NULL, NULL);

    DefineCustomRealVariable("planet.grid_gco2_per_kwh",
                             "Grid carbon intensity (gCO2e per kWh).",
                             NULL, &planet_grid_gco2_per_kwh, 400.0,
                             0.0, 5000.0,
                             PGC_USERSET, 0, NULL, NULL, NULL);

    DefineCustomRealVariable("planet.embodied_gco2_per_byte",
                             "Embodied storage carbon per byte (gCO2e/byte).",
                             NULL, &planet_embodied_gco2_per_byte, 1.5e-8,
                             0.0, 1.0,
                             PGC_USERSET, 0, NULL, NULL, NULL);

    DefineCustomRealVariable("planet.idle_watts_per_byte",
                             "Idle storage power per byte (W/byte).",
                             NULL, &planet_idle_watts_per_byte, 2.5e-12,
                             0.0, 1.0,
                             PGC_USERSET, 0, NULL, NULL, NULL);

    MarkGUCPrefixReserved("planet");

    /* Install hooks. */
    prev_ExecutorStart  = ExecutorStart_hook;
    ExecutorStart_hook  = planet_ExecutorStart;
    prev_ExecutorRun    = ExecutorRun_hook;
    ExecutorRun_hook    = planet_ExecutorRun;
    prev_ExecutorFinish = ExecutorFinish_hook;
    ExecutorFinish_hook = planet_ExecutorFinish;
    prev_ExecutorEnd    = ExecutorEnd_hook;
    ExecutorEnd_hook    = planet_ExecutorEnd;

    MemSet(&st, 0, sizeof(st));
}

void
_PG_fini(void)
{
    ExecutorStart_hook  = prev_ExecutorStart;
    ExecutorRun_hook    = prev_ExecutorRun;
    ExecutorFinish_hook = prev_ExecutorFinish;
    ExecutorEnd_hook    = prev_ExecutorEnd;
}

/* ---- SQL-callable getters for the last top-level query ------------------- */

PG_FUNCTION_INFO_V1(planet_last_carbon_g);
PG_FUNCTION_INFO_V1(planet_last_energy_joules);
PG_FUNCTION_INFO_V1(planet_last_cpu_seconds);
PG_FUNCTION_INFO_V1(planet_last_wall_seconds);
PG_FUNCTION_INFO_V1(planet_last_awake_g);
PG_FUNCTION_INFO_V1(planet_last_blks_read);
PG_FUNCTION_INFO_V1(planet_last_blks_written);
PG_FUNCTION_INFO_V1(planet_last_wal_bytes);
PG_FUNCTION_INFO_V1(planet_last_compute_g);
PG_FUNCTION_INFO_V1(planet_last_io_g);
PG_FUNCTION_INFO_V1(planet_last_wal_g);
PG_FUNCTION_INFO_V1(planet_reset);

Datum
planet_last_carbon_g(PG_FUNCTION_ARGS)
{
    if (!st.valid)
        PG_RETURN_NULL();
    PG_RETURN_FLOAT8(st.carbon_g);
}

Datum
planet_last_energy_joules(PG_FUNCTION_ARGS)
{
    if (!st.valid)
        PG_RETURN_NULL();
    PG_RETURN_FLOAT8(st.energy_j);
}

Datum
planet_last_cpu_seconds(PG_FUNCTION_ARGS)
{
    if (!st.valid)
        PG_RETURN_NULL();
    PG_RETURN_FLOAT8(st.cpu_seconds);
}

Datum
planet_last_wall_seconds(PG_FUNCTION_ARGS)
{
    if (!st.valid)
        PG_RETURN_NULL();
    PG_RETURN_FLOAT8(st.wall_seconds);
}

Datum
planet_last_awake_g(PG_FUNCTION_ARGS)
{
    if (!st.valid)
        PG_RETURN_NULL();
    PG_RETURN_FLOAT8(st.cg_awake);
}

Datum
planet_last_blks_read(PG_FUNCTION_ARGS)
{
    if (!st.valid)
        PG_RETURN_NULL();
    PG_RETURN_INT64(st.blks_read);
}

Datum
planet_last_blks_written(PG_FUNCTION_ARGS)
{
    if (!st.valid)
        PG_RETURN_NULL();
    PG_RETURN_INT64(st.blks_written);
}

Datum
planet_last_wal_bytes(PG_FUNCTION_ARGS)
{
    if (!st.valid)
        PG_RETURN_NULL();
    PG_RETURN_INT64((int64) st.wal_bytes);
}

Datum
planet_last_compute_g(PG_FUNCTION_ARGS)
{
    if (!st.valid)
        PG_RETURN_NULL();
    PG_RETURN_FLOAT8(st.cg_compute);
}

Datum
planet_last_io_g(PG_FUNCTION_ARGS)
{
    if (!st.valid)
        PG_RETURN_NULL();
    PG_RETURN_FLOAT8(st.cg_io);
}

Datum
planet_last_wal_g(PG_FUNCTION_ARGS)
{
    if (!st.valid)
        PG_RETURN_NULL();
    PG_RETURN_FLOAT8(st.cg_wal);
}

Datum
planet_reset(PG_FUNCTION_ARGS)
{
    st.valid = false;
    st.capturing = false;
    PG_RETURN_VOID();
}
