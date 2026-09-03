"""Every model call this system makes, recorded as a row.

Why this exists, and why in ClickHouse rather than a log file.

A continuity report is a set of claims about someone's shoot, and a crew will act on them:
reset a prop, go again, or let a take stand. Claims that cannot be interrogated afterwards
are not evidence. So the system keeps its own trace: which model ran, on which frame, how
long it took, what it concluded, and how sure it was. When a report says the mug was on the
wrong side in take three, the run that decided that is a row you can go and read.

That also happens to be the workload ClickHouse is built for and, since the Langfuse
acquisition, the one it has organised itself around: high-ingest, append-only, immutable
observations, high-cardinality fields, no joins. A shoot day is not 60 rows. It is a check
every few seconds across every take, across every shooting day of a production, and the
interesting signal is the outlier rather than the average. Sampling it away would delete the
evidence, which is exactly the thing that makes the report worth anything.

Writes are fire-and-forget and failures are swallowed. Telemetry must never be able to take
down a live check that a crew is standing around waiting for.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from pipeline.store import clickhouse_config

# One immutable row per model call, whatever the caller was doing. Denormalized for the same
# reason the observations table is: every question worth asking here is a filter and a
# group-by over one table.
#
# ORDER BY starts with the operation because the questions are per-kind ("how fast is the
# live check", "how often does the presence pass recover something"), then time, because
# everything else is a window over a shoot.
RUNS_SQL = """
CREATE TABLE IF NOT EXISTS agent_runs
(
    ts              DateTime64(3) DEFAULT now64(3),
    operation       LowCardinality(String),
    scene_id        LowCardinality(String),
    take_id         LowCardinality(String),
    model           LowCardinality(String),
    latency_ms      UInt32,
    outcome         LowCardinality(String),
    findings        UInt16,
    entities        Array(String),
    queries         Array(String),
    detail          String
)
ENGINE = MergeTree
ORDER BY (operation, ts)
TTL toDateTime(ts) + INTERVAL 180 DAY
"""


@dataclass
class Run:
    operation: str          # live_check | extract | reconcile | presence | ask
    model: str
    latency_ms: int
    outcome: str            # holds | divergence | error | ok
    scene_id: str = ""
    take_id: str = ""
    findings: int = 0
    entities: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    detail: str = ""

    def to_row(self) -> dict:
        row = asdict(self)
        row["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        row["detail"] = (row.get("detail") or "")[:2000]
        return row


def _post(cfg: dict, sql: str, body: str | None = None) -> None:
    params = {"database": cfg.get("database", "default")}
    if body is not None:
        params["query"] = sql
        payload = body.encode("utf-8")
    else:
        payload = sql.encode("utf-8")

    url = f"https://{cfg['host']}:{cfg['port']}/?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, data=payload, method="POST")
    token = base64.b64encode(f"{cfg['username']}:{cfg['password']}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    urllib.request.urlopen(request, timeout=20).read()


_schema_ready = False
_lock = threading.Lock()


def _ensure_schema(cfg: dict) -> None:
    global _schema_ready
    with _lock:
        if _schema_ready:
            return
        try:
            _post(cfg, RUNS_SQL)
            _schema_ready = True
        except Exception:
            # Leave it unset so a later call retries. A telemetry table that does not exist
            # yet is not a reason to fail the work the user is waiting on.
            pass


def record(run: Run) -> None:
    """Write one run. Never raises, never blocks the caller."""
    cfg = clickhouse_config()
    if not cfg or not cfg.get("password"):
        return

    def _write() -> None:
        try:
            _ensure_schema(cfg)
            _post(cfg, "INSERT INTO agent_runs FORMAT JSONEachRow", json.dumps(run.to_row()))
        except Exception:
            pass

    threading.Thread(target=_write, daemon=True).start()


# Questions this table is here to answer. Kept as SQL rather than prose so they can be run
# rather than believed, and so the UI and the writeup cannot drift from each other.
INSIGHT_QUERIES = {
    "live_latency_percentiles": """
        SELECT model,
               count() AS checks,
               round(quantile(0.5)(latency_ms)) AS p50_ms,
               round(quantile(0.9)(latency_ms)) AS p90_ms,
               round(max(latency_ms)) AS max_ms
        FROM agent_runs
        WHERE operation = 'live_check'
        GROUP BY model
        ORDER BY checks DESC
    """,
    "divergence_rate": """
        SELECT countIf(outcome = 'divergence') AS flagged,
               countIf(outcome = 'holds') AS held,
               round(100 * countIf(outcome = 'divergence') / count(), 1) AS flagged_pct
        FROM agent_runs
        WHERE operation = 'live_check'
    """,
    "most_flagged_entities": """
        SELECT arrayJoin(entities) AS entity, count() AS times_flagged
        FROM agent_runs
        WHERE operation = 'live_check' AND outcome = 'divergence'
        GROUP BY entity
        ORDER BY times_flagged DESC
        LIMIT 10
    """,
    "fallback_usage": """
        SELECT model, count() AS calls
        FROM agent_runs
        GROUP BY model
        ORDER BY calls DESC
    """,
}
