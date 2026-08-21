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

from pydantic import BaseModel  # noqa: E402

from pipeline.compare import compare  # noqa: E402
from pipeline.store import JsonStore, load_env, to_dict  # noqa: E402

load_env()

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


class Question(BaseModel):
    question: str
    scene_id: str | None = None


@app.post("/api/ask")
async def ask_question(q: Question):
    """Natural language in, answer plus the SQL the agent chose, out.

    The queries come back with the answer on purpose. A continuity note that cannot be
    checked is not evidence, and anyone technical will want to see what was actually run
    before they believe a claim about their own shoot.
    """
    if not q.question.strip():
        raise HTTPException(status_code=400, detail="Empty question")

    # Imported here so the rest of the app starts and serves with no ClickHouse, no MCP
    # server and no API key present.
    from pipeline.ask import ask_async

    try:
        result = await ask_async(q.question.strip(), q.scene_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    return result.to_dict()


class LiveFrame(BaseModel):
    frame: str  # base64 jpeg, or a data: URL
    scene_id: str
    reference_take: str | None = None
    scene_context: str | None = None


@app.post("/api/live/check")
async def live_check(payload: LiveFrame):
    """One frame from a rolling take, checked against a reference take.

    Stateless on purpose. A live check that depends on having seen the previous frame
    cannot recover from a dropped connection, and sets drop connections constantly.
    """
    from pipeline.live import check_frame_async, decode_frame

    takes = store.get_takes(payload.scene_id)
    if not takes:
        raise HTTPException(status_code=404, detail=f"No scene '{payload.scene_id}'")

    ref_id = payload.reference_take or takes[0].take_id
    observations = [
        to_dict(o) for o in store.get_observations(payload.scene_id) if o.take_id == ref_id
    ]
    if not observations:
        raise HTTPException(status_code=404, detail=f"No reference state for take '{ref_id}'")

    try:
        result = await check_frame_async(
            decode_frame(payload.frame), observations, payload.scene_context
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    return {**result.to_dict(), "reference_take": ref_id}


@app.get("/api/live/reference/{scene_id}")
def live_reference(scene_id: str, take_id: str | None = None):
    """What the operator is being held to, so the live view can show it alongside."""
    takes = store.get_takes(scene_id)
    if not takes:
        raise HTTPException(status_code=404, detail=f"No scene '{scene_id}'")
    ref_id = take_id or takes[0].take_id
    observations = [o for o in store.get_observations(scene_id) if o.take_id == ref_id]
    return {
        "scene_id": scene_id,
        "reference_take": ref_id,
        "available_takes": [t.take_id for t in takes],
        "observations": [to_dict(o) for o in observations],
    }


@app.get("/live")
def live_page():
    return FileResponse(STATIC / "live.html")


@app.get("/api/capabilities")
def capabilities():
    """What this deployment can actually do, so the UI never offers a dead control."""
    return {
        "ask": bool(
            (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
            and os.environ.get("CLICKHOUSE_HOST")
        )
    }


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
