"""Create the tables and load scene state into ClickHouse.

Uses the HTTP interface over the standard library rather than a client package. Two reasons:
a judge cloning this repo should not need a database driver installed to read the loader and
understand what it does, and the insert path is deliberately boring. The interesting
ClickHouse work in this project is the query path, which goes through the MCP server so the
model can plan and issue its own SQL.

Idempotent: tables are created IF NOT EXISTS and a scene's rows are deleted before reload, so
running this twice does not double every observation. That matters because the whole product
rests on counting how many takes agree, and silent duplicates would make three takes look
like six.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.store import (  # noqa: E402
    SCHEMA_SQL,
    TAKES_SQL,
    JsonStore,
    clickhouse_config,
    load_env,
)


class ClickHouseHTTP:
    def __init__(self, cfg: dict) -> None:
        self.url = f"https://{cfg['host']}:{cfg['port']}"
        self.auth = f"{cfg['username']}:{cfg['password']}"
        self.database = cfg.get("database", "default")

    def execute(self, sql: str, body: str | None = None) -> str:
        params = {"database": self.database}
        if body is not None:
            params["query"] = sql
            payload = body.encode("utf-8")
        else:
            payload = sql.encode("utf-8")

        url = f"{self.url}/?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, data=payload, method="POST")

        import base64

        token = base64.b64encode(self.auth.encode()).decode()
        request.add_header("Authorization", f"Basic {token}")

        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            raise RuntimeError(f"ClickHouse {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Could not reach ClickHouse at {self.url}. "
                f"The service may be idle or asleep. Underlying error: {exc.reason}"
            ) from exc


def rows_to_jsoneachrow(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in rows)


def load_scene(client: ClickHouseHTTP, store: JsonStore, scene_id: str) -> dict:
    takes = store.get_takes(scene_id)
    observations = store.get_observations(scene_id)
    if not takes:
        raise ValueError(f"No takes found for scene '{scene_id}'")

    # Replace rather than append. See module docstring: duplicated observations would corrupt
    # the majority counts the continuity comparison depends on.
    client.execute(f"ALTER TABLE observations DELETE WHERE scene_id = '{scene_id}'")
    client.execute(f"ALTER TABLE takes DELETE WHERE scene_id = '{scene_id}'")

    take_rows = [
        {
            "scene_id": scene_id,
            "take_id": t.take_id,
            "scene_summary": t.scene_summary,
            "duration_s": t.duration_s,
            "frames_used": t.frames_used,
            "model": t.model,
        }
        for t in takes
    ]
    obs_rows = [
        {
            "scene_id": scene_id,
            "take_id": o.take_id,
            "entity": o.entity,
            "category": o.category,
            "position_h": o.position_h,
            "depth": o.depth,
            "state": o.state,
            "relative_to": o.relative_to,
            "moved_during_take": 1 if o.moved_during_take else 0,
            "confidence": o.confidence,
            "via": o.via,
            "seen_at_timestamp": o.seen_at_timestamp,
            "where_in_frame": o.where_in_frame,
        }
        for o in observations
    ]

    client.execute("INSERT INTO takes FORMAT JSONEachRow", rows_to_jsoneachrow(take_rows))
    client.execute("INSERT INTO observations FORMAT JSONEachRow", rows_to_jsoneachrow(obs_rows))

    return {"scene": scene_id, "takes": len(take_rows), "observations": len(obs_rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "out", help="pipeline output dir")
    parser.add_argument("--scene", default=None, help="one scene, or all if omitted")
    args = parser.parse_args()

    load_env()
    cfg = clickhouse_config()
    if not cfg:
        print(
            "\nCLICKHOUSE_HOST is not set. Copy .env.example to .env and fill it in.\n",
            file=sys.stderr,
        )
        return 1

    client = ClickHouseHTTP(cfg)
    store = JsonStore(args.out)

    try:
        client.execute(SCHEMA_SQL)
        client.execute(TAKES_SQL)
        print(f"tables ready on {cfg['host']}")

        scenes = [args.scene] if args.scene else [s.scene_id for s in store.list_scenes()]
        if not scenes:
            print(f"No scenes found under {args.out}", file=sys.stderr)
            return 1

        total = {"takes": 0, "observations": 0}
        for scene_id in scenes:
            result = load_scene(client, store, scene_id)
            total["takes"] += result["takes"]
            total["observations"] += result["observations"]
            print(f"  {scene_id}: {result['takes']} takes, {result['observations']} observations")

        print(f"\nloaded {total['takes']} takes and {total['observations']} observations")
    except (RuntimeError, ValueError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
