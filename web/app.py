"""The surface a judge actually touches.

The hackathon requires a hosted URL that can be tested, and judges have no smart glasses. So
this ships with a shoot already loaded: the footage is how the data got here, not something a
reviewer needs to reproduce. Everything below is served from scene state the pipeline already
produced.

Runs on the JSON store with no credentials at all, and on ClickHouse when configured. That is
not hedging. A reviewer opening this on a Sunday should not meet a connection error because a
database went to sleep, and a demo that only works when five services are up is a demo that
does not work.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.compare import compare  # noqa: E402
from pipeline.store import JsonStore, to_dict  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
OUT_DIR = Path(os.environ.get("DAILIES_OUT", ROOT / "out"))

app = FastAPI(
    title="Dailies",
    description="Continuity records from smart-glasses POV footage.",
    version="0.1.0",
)

store = JsonStore(OUT_DIR)


def _states_for_compare(scene_id: str) -> dict[str, dict]:
    """compare() wants raw state dicts, so rebuild them from the store's flat rows."""
    takes = store.get_takes(scene_id)
    if not takes:
        raise HTTPException(status_code=404, detail=f"No scene '{scene_id}'")
    observations = store.get_observations(scene_id)

    states: dict[str, dict] = {
        t.take_id: {"take_id": t.take_id, "scene_summary": t.scene_summary, "observations": []}
        for t in takes
    }
    for obs in observations:
        if obs.take_id in states:
            states[obs.take_id]["observations"].append(to_dict(obs))
    return states


@app.get("/api/health")
def health():
    scenes = store.list_scenes()
    return {
        "status": "ok",
        "backend": "json",
        "scenes": len(scenes),
        "takes": sum(s.take_count for s in scenes),
        "observations": sum(s.observation_count for s in scenes),
    }


@app.get("/api/scenes")
def list_scenes():
    return [to_dict(s) for s in store.list_scenes()]


@app.get("/api/scenes/{scene_id}/takes")
def get_takes(scene_id: str):
    takes = store.get_takes(scene_id)
    if not takes:
        raise HTTPException(status_code=404, detail=f"No scene '{scene_id}'")
    observations = store.get_observations(scene_id)

    by_take: dict[str, list] = {t.take_id: [] for t in takes}
    for obs in observations:
        by_take.setdefault(obs.take_id, []).append(to_dict(obs))

    return [{**to_dict(t), "observations": by_take.get(t.take_id, [])} for t in takes]


@app.get("/api/scenes/{scene_id}/continuity")
def get_continuity(scene_id: str):
    """The payoff: what actually differs between takes, and what only looks like it does."""
    states = _states_for_compare(scene_id)
    if len(states) < 2:
        return {"takes": list(states), "deltas": [], "missing": [], "camera_shifts": []}

    deltas, missing, shifts = compare(states)
    return {
        "takes": sorted(states),
        "deltas": [
            {
                "entity": d.entity,
                "field": d.field,
                "expected": d.expected,
                "outliers": d.outliers,
                "agreeing_takes": d.agreeing_takes,
                "severity": d.severity,
            }
            for d in deltas
        ],
        "missing": [
            {"entity": m.entity, "present_in": m.present_in, "absent_from": m.absent_from}
            for m in missing
        ],
        "camera_shifts": [
            {
                "take_id": s.take_id,
                "field": s.field,
                "from": s.from_value,
                "to": s.to_value,
                "entities": s.entities,
            }
            for s in shifts
        ],
    }


@app.get("/api/scenes/{scene_id}/entities")
def get_entities(scene_id: str):
    """Every object in the scene and where it sat in each take. The facing page, as data."""
    observations = store.get_observations(scene_id)
    if not observations:
        raise HTTPException(status_code=404, detail=f"No scene '{scene_id}'")

    entities: dict[str, dict] = {}
    for obs in observations:
        entry = entities.setdefault(
            obs.entity, {"entity": obs.entity, "category": obs.category, "per_take": {}}
        )
        entry["per_take"][obs.take_id] = {
            "position_h": obs.position_h,
            "depth": obs.depth,
            "state": obs.state,
            "confidence": obs.confidence,
            "via": obs.via,
        }
    return sorted(entities.values(), key=lambda e: e["entity"])


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
