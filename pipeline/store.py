"""Where scene state lives, behind one interface.

Two implementations, on purpose:

- `JsonStore` reads the files the pipeline already writes. It needs no network, no
  credentials and no database, which means the web app can be developed and a judge can run
  it locally even if a hosted database is asleep or a trial has lapsed.
- `ClickHouseStore` is the real one. Observations are a columnar, append-only fact table
  queried by entity and take, which is exactly the shape ClickHouse is for.

The interface is narrow deliberately. Everything above this file speaks in scenes, takes and
observations, so swapping the backing store cannot ripple into the UI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Observation:
    take_id: str
    entity: str
    category: str
    position_h: str
    depth: str
    state: str
    state_class: str
    state_value: str
    relative_to: str
    moved_during_take: bool
    confidence: float
    # Set when the observation came from the second look rather than the first pass. Kept
    # visible rather than hidden: a record recovered by re-examination is a different kind of
    # claim from one the first pass volunteered, and a human auditing a continuity report
    # deserves to know which is which.
    via: str = ""
    seen_at_timestamp: float = -1.0
    where_in_frame: str = ""


@dataclass(frozen=True)
class Take:
    take_id: str
    scene_id: str
    scene_summary: str
    duration_s: float
    frames_used: int
    model: str


@dataclass(frozen=True)
class Scene:
    scene_id: str
    take_count: int
    observation_count: int


class Store(Protocol):
    def list_scenes(self) -> list[Scene]: ...
    def get_takes(self, scene_id: str) -> list[Take]: ...
    def get_observations(self, scene_id: str) -> list[Observation]: ...


# --------------------------------------------------------------------------------------
# JSON, for local development and offline judging
# --------------------------------------------------------------------------------------


class JsonStore:
    """Reads the pipeline's own output directory.

    Layout it expects, which is what ingest/extract/reconcile/presence already produce:

        out/<scene_id>/<take_id>.json                  ingest manifest
        out/<scene_id>/reconciled/<take_id>.state.json reconciled scene state
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _state_dir(self, scene_id: str) -> Path:
        # Prefer reconciled state. Raw state is pre-reconciliation and produces the phantom
        # continuity breaks the reconciliation pass exists to remove, so it is a fallback
        # only, never a silent default.
        reconciled = self.root / scene_id / "reconciled"
        return reconciled if reconciled.is_dir() else self.root / scene_id

    def _load(self, scene_id: str) -> list[dict]:
        state_dir = self._state_dir(scene_id)
        if not state_dir.is_dir():
            return []
        out = []
        for path in sorted(state_dir.glob("*.state.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def _duration(self, scene_id: str, take_id: str) -> float:
        manifest = self.root / scene_id / f"{take_id}.json"
        if not manifest.exists():
            return 0.0
        try:
            return float(json.loads(manifest.read_text(encoding="utf-8")).get("duration_s", 0.0))
        except (json.JSONDecodeError, OSError, TypeError):
            return 0.0

    def list_scenes(self) -> list[Scene]:
        if not self.root.is_dir():
            return []
        scenes = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            states = self._load(child.name)
            if not states:
                continue
            scenes.append(
                Scene(
                    scene_id=child.name,
                    take_count=len(states),
                    observation_count=sum(len(s.get("observations", [])) for s in states),
                )
            )
        return scenes

    def get_takes(self, scene_id: str) -> list[Take]:
        return [
            Take(
                take_id=s["take_id"],
                scene_id=scene_id,
                scene_summary=s.get("scene_summary", ""),
                duration_s=self._duration(scene_id, s["take_id"]),
                frames_used=int(s.get("frames_used", 0)),
                model=s.get("model", ""),
            )
            for s in self._load(scene_id)
        ]

    def get_observations(self, scene_id: str) -> list[Observation]:
        out = []
        for state in self._load(scene_id):
            for obs in state.get("observations", []):
                out.append(
                    Observation(
                        take_id=state["take_id"],
                        entity=obs.get("entity", ""),
                        category=obs.get("category", ""),
                        position_h=obs.get("position_h", ""),
                        depth=obs.get("depth", ""),
                        state=obs.get("state", ""),
                        state_class=obs.get("state_class", "none"),
                        state_value=obs.get("state_value", "na"),
                        relative_to=obs.get("relative_to", ""),
                        moved_during_take=bool(obs.get("moved_during_take", False)),
                        confidence=float(obs.get("confidence", 0.0)),
                        via=obs.get("via", ""),
                        seen_at_timestamp=float(obs.get("seen_at_timestamp", -1.0)),
                        where_in_frame=obs.get("where_in_frame", ""),
                    )
                )
        return out


# --------------------------------------------------------------------------------------
# Schema shared by the ClickHouse path
# --------------------------------------------------------------------------------------

# One row per observed entity per take. Denormalized on purpose: every question this product
# asks ("which takes have the mug on the left", "what changed between takes") is a filter and
# a group-by over this single table, and a star schema would buy nothing but joins.
#
# ORDER BY puts scene first because every query is scoped to a scene, then entity because
# comparing one object across takes is the core access pattern.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS observations
(
    scene_id           LowCardinality(String),
    take_id            LowCardinality(String),
    entity             LowCardinality(String),
    category           LowCardinality(String),
    position_h         LowCardinality(String),
    depth              LowCardinality(String),
    state              String,
    state_class        LowCardinality(String),
    state_value        LowCardinality(String),
    relative_to        String,
    moved_during_take  UInt8,
    confidence         Float32,
    via                LowCardinality(String),
    seen_at_timestamp  Float32,
    where_in_frame     String,
    ingested_at        DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (scene_id, entity, take_id)
"""

TAKES_SQL = """
CREATE TABLE IF NOT EXISTS takes
(
    scene_id       LowCardinality(String),
    take_id        LowCardinality(String),
    scene_summary  String,
    duration_s     Float32,
    frames_used    UInt16,
    model          LowCardinality(String),
    ingested_at    DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (scene_id, take_id)
"""


def observation_rows(scene_id: str, observations: list[Observation]) -> list[list]:
    """Flatten to positional rows, column order matching SCHEMA_SQL."""
    return [
        [
            scene_id,
            o.take_id,
            o.entity,
            o.category,
            o.position_h,
            o.depth,
            o.state,
            o.state_class,
            o.state_value,
            o.relative_to,
            1 if o.moved_during_take else 0,
            o.confidence,
            o.via,
            o.seen_at_timestamp,
            o.where_in_frame,
        ]
        for o in observations
    ]


def take_rows(takes: list[Take]) -> list[list]:
    return [
        [t.scene_id, t.take_id, t.scene_summary, t.duration_s, t.frames_used, t.model]
        for t in takes
    ]


def load_env(path: Path | None = None) -> None:
    """Read a .env into os.environ without adding a dependency for it.

    Existing environment variables win, so a shell export or a Cloud Run secret is never
    silently overridden by a stale file left in a working copy.
    """
    env_path = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def clickhouse_config() -> dict | None:
    """Connection settings from the environment, or None if not configured."""
    host = os.environ.get("CLICKHOUSE_HOST")
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("CLICKHOUSE_PORT", "8443")),
        "username": os.environ.get("CLICKHOUSE_USER", "default"),
        "password": os.environ.get("CLICKHOUSE_PASSWORD", ""),
        "database": os.environ.get("CLICKHOUSE_DATABASE", "default"),
    }


def to_dict(obj) -> dict:
    return asdict(obj)
