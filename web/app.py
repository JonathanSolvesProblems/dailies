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
            "state_class": obs.state_class,
            "state_value": obs.state_value,
            "confidence": obs.confidence,
            "via": obs.via,
        }
    return sorted(entities.values(), key=lambda e: e["entity"])


def _explain(exc: BaseException, _depth: int = 0) -> str:
    """Flatten an exception into something that names the actual cause.

    The MCP client runs its stdio transport inside an anyio task group, so anything that
    fails underneath surfaces as `ExceptionGroup: unhandled errors in a TaskGroup (1
    sub-exception)`. That string is the same whether ClickHouse refused the password, the
    subprocess died, or the model was rate limited, which makes a 500 from this endpoint
    impossible to act on. It cost a debugging session to learn that the hard way.

    Both the group's children and the `__cause__` chain are walked, because the useful
    detail is usually one or two levels below whatever reached the handler.
    """
    if _depth > 4:
        return type(exc).__name__
    inner = list(getattr(exc, "exceptions", None) or [])
    if inner:
        return " | ".join(_explain(e, _depth + 1) for e in inner[:3])
    text = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return f"{text} <- {_explain(cause, _depth + 1)}"
    return text


def _find(exc: BaseException, kind: type, _depth: int = 0) -> BaseException | None:
    """Search an exception, its group children and its cause chain for a given type.

    `except RuntimeError` does not catch a RuntimeError raised inside an anyio task group,
    because what propagates is an ExceptionGroup wrapping it. The ask path raises RuntimeError
    to mean "upstream is rate limited, this is not the server's fault", which should be a 503;
    it was reaching the client as a 500 because the isinstance check never saw past the
    wrapper.
    """
    if _depth > 4:
        return None
    if isinstance(exc, kind):
        return exc
    for sub in list(getattr(exc, "exceptions", None) or []):
        found = _find(sub, kind, _depth + 1)
        if found is not None:
            return found
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _find(cause, kind, _depth + 1)
    return None


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
    except Exception as exc:
        # Look inside the task group before deciding the status. Upstream being rate limited
        # is a 503 the caller can retry, not a 500 that says the app is broken.
        upstream = _find(exc, RuntimeError)
        if upstream is not None:
            raise HTTPException(status_code=503, detail=str(upstream)) from exc
        raise HTTPException(status_code=500, detail=_explain(exc)) from exc

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
    """What this deployment can actually do, so the UI never offers a dead control.

    Model access must be checked through the same helper the request path uses. This asked
    only for an API key, which quietly became wrong the moment the deployment moved to Vertex
    AI: with no key set the endpoint reported ask=false and the UI hid the question box, so
    the one feature the ClickHouse track is judged on would have been invisible on a service
    that could answer perfectly well. A capability probe that disagrees with the code it
    describes is worse than no probe.
    """
    from pipeline.client import describe, use_vertex, vertex_project

    model_ready = bool(
        (use_vertex() and vertex_project())
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
    )
    return {
        "ask": bool(model_ready and os.environ.get("CLICKHOUSE_HOST")),
        "model": describe(),
    }


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
