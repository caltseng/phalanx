# PH-03 local operating envelope

## Decision for PH-04

Use **CPython 3.14**, **FastAPI**, **plain Uvicorn**, and Python's standard-library **`sqlite3`** with explicit SQL migrations. Lock runtime and test dependencies when the service lands. Use standard-library `unittest` and an HTTP smoke test from a separate process. FastAPI supplies request validation and an OpenAPI contract. Uvicorn runs the same app on Windows and on a hosted Linux instance. A single process with one local database file is sufficient to bootstrap the API. [FastAPI deployment documentation](https://fastapi.tiangolo.com/deployment/manually/)

Start with rollback journaling (`journal_mode=DELETE`), `synchronous=EXTRA`, a two-second busy timeout, and foreign-key enforcement on every connection. Configure connection-local settings when opening each connection. Keep connections thread-owned, keep writes short, and serialize write transactions within the service process from the start. Use `BEGIN IMMEDIATE` for read-modify-write transactions. Keep HTTP and provider calls outside transactions. For backup, use the SQLite online backup API and verify restore into a separate path. Use local persistent disk with one service instance owning the database. The Windows runtime tested here bundled SQLite 3.50.4. Before switching to WAL, verify the loaded library includes SQLite's WAL-reset fix and rerun contention and recovery tests. [Python sqlite3 reference](https://docs.python.org/3.14/library/sqlite3.html) · [SQLite durability settings](https://sqlite.org/pragma.html#pragma_synchronous) · [SQLite WAL-reset fix](https://sqlite.org/wal.html#the_wal_reset_bug)

Suggested PH-04 entry points, once the service exists. Use a full Python install with venv and pip. Install FastAPI and Uvicorn from the project's locked dependencies. The pilot used a portable runtime to work around an inaccessible Store Python alias.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m uvicorn phalanx.server:app --host 127.0.0.1 --port 8000
# In a separate terminal
Invoke-RestMethod http://127.0.0.1:8000/health
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

## Measured baseline

Windows 11 Pro build 26200, AMD Ryzen 9 5900HS (8 cores, 16 logical processors), 39.4 GiB visible RAM, 15.9 GiB free physical RAM and 553 GiB free on C: at the sample. The six tracked Phalanx files totaled 90,558 bytes at the pilot base. Five launches of the portable CPython 3.14.9 process importing `sqlite3` took 204–283 ms (median 226 ms). Those are process startup timings. This repo has no build workload at the pilot base. Per-chat startup, marginal memory, and representative build CPU, memory and disk use remain unmeasured. The real Codex worker limit is unverified. PH-01 can supply visible-chat startup samples and PH-19 should measure their marginal footprint and representative builds on the intended repos.

### SQLite queue probe

The local NTFS probe used separate connections per synthetic client thread and included transaction begin, writes and durable commit in each timed operation. It seeded 1% ready, 29% blocked and 70% closed jobs, plus two history rows per job carrying 100 bytes of text each. The ready index was partial on `(score DESC, ready_since, id) WHERE status='ready'`; the candidate query used the same ordering and `LIMIT 1`. SQLite reported a covering scan of that partial index. The score was seeded and fixed. A claim selected the first ready job, marked it running and appended history in one transaction. Renewal updated that job by ID and expected status and lease. Release checked the lease, closed the job and appended history. No timed operation scanned the history table.

| Jobs / history | Clients and pace | Claim p50 / p95 / p99 | Renewal p50 / p95 / p99 | Outcome |
| --- | --- | --- | --- | --- |
| 100k / 200k | 4, 5 ms between transactions per client | 13.85 / 82.71 / 623.88 ms | 14.81 / 125.79 / 902.99 ms | 480 writes in 6.83 s, including 160 distinct claims. Database 24.3 MiB before test. |
| 100k / 200k | 16, 5 ms between transactions per client | — | — | Run failed when a claim exceeded the two-second lock timeout. |
| 1M / 2M | 16, 180 ms between transactions per client, no application lock | — | — | A claim exceeded the same two-second lock timeout. |
| 1M / 2M | 16, 180 ms between transactions per client, 12 ms stagger per client, serialized writes | 70.77 / 344.37 / 381.17 ms | 57.75 / 131.05 / 377.34 ms | 480 writes in 8.27 s (58/s), including 160 distinct claims. Database 244.9 MiB before test. |

Completed runs checked for duplicate claims and leftover running jobs, then ran `PRAGMA quick_check`. The probe bypassed HTTP, used a single ready pool, and omitted auth, capacity checks, dynamic aging, competing board reads, real worker activity and retry recovery. The serialized trial completed, with writer-lock wait included in the measured latency. At about 58 completed writes/s, both p95 values exceeded the proposed HTTP p95 target before adding HTTP overhead. Use PH-06 to test bounded admission, renewal priority under pressure, and the sustainable write rate. The serialized and unserialized million-row runs also used different startup staggering, so these samples do not isolate the effect of the application lock.

## Provisional defaults and targets

| Setting | Starting value |
| --- | --- |
| Local worker slots | 4 configurable slots. Count live or uncertain sessions against capacity. Validate 2, 4 and 8 real sessions before raising the default. |
| Claim pacing | Poll every 2 s with ±20% jitter when dispatch is enabled and capacity is available. Nudge on a known slot release or eligibility event. Back off empty polls to 10 s. |
| Leases | 90 s expiry, renew every 20 s with jitter. Schedule heartbeats independently from chat observation and batch work. |
| Priority aging | Four initial bands (0 through 3). Add one rank per 5 minutes continuously eligible, capped at rank 3. Break ties by oldest eligible time then stable ID. Preserve age across contention and track retry delays separately. Use the oldest ready head per band if that keeps ranking indexed. |
| Retries and external waits | Start transient stage retry delay at 5 s, double with jitter to a 2 min cap, and surface after five failed attempts. For ambiguous provider writes, reconcile before retrying. Start external reconciliation at 30 s with jitter, respect provider retry timing and bound each due batch to 100. |

At the paced synthetic load below, target claim and renewal HTTP p95 ≤100 ms and p99 ≤500 ms, including queueing and durable commit. Separately target p95 ≤3 s from eligibility to claim while a matching slot is free. Under urgent intake, a continuously eligible oldest item should reach top rank within 15 min and claim on the next available matching slot and poll once older top-rank work has drained. Measure rank-promotion and oldest wait separately from time spent blocked or at capacity.

After restart, target healthy HTTP and durable queue reads within 10 s, dispatch of eligible unrelated work within 30 s when slots are available, and reconciliation of 10,000 due local events within 2 min while serving renewals. Time provider-directed delays separately. For the real-worker sweep, retain at least 8 GiB available RAM, 20% disk free on the volume holding worktrees and the database, and at least 20% CPU idle on a rolling five-minute average. Record peak disk usage per build and stop raising concurrency when any headroom target fails.

## Load and recovery gates

- PH-06 should sweep 100k and 1M jobs with the seed proportions above and twice as many history rows. Use 4, 16 and 32 synthetic clients, paced at 80 aggregate operations/s as a target and with simultaneous bursts. Mix claims and completions with renewals and paginated board reads. Record offered and completed rates, lock and retry counts, latency percentiles per operation, query plans and oldest eligible age. Require zero duplicate claims, stale lease writes or oversubscription.
- PH-19 should add 10,000 due external waits, prerequisite fan-out, urgent arrivals, a provider throttle and a storage outage. Restart during a dependent wakeup batch. Interrupt a worker while another item is waiting for a blocked prerequisite. Verify unaffected items advance and each affected item resumes from recorded state once its trigger is satisfied.
- On the intended two repos, measure cold and warm worker launch to first observable progress, sampled system memory and incremental memory with 2, 4 and 8 visible workers, and wall time, CPU and peak disk delta for each repo's normal checks. Include parallel builds, preview lifetimes, worktrees and shared caches. Count uncertain sessions until reconciled. The synthetic client counts measure database contention only.
