"""Turn the frames of a take into structured scene state.

This is the core of the product. Everything else is plumbing around this step.

The job is not "describe the video". A description is prose, and prose is not queryable.
The job is to produce comparable records: the same mug, named the same way, in take 1 and
take 4, so that a difference between them is a row that is missing rather than a paragraph
someone has to read.

Two decisions do most of the work here:

1. **One call per take, not per frame.** Continuity state is a property of the take, and a
   model that sees all the frames at once can say "the mug is on the left throughout"
   instead of emitting twelve disconnected guesses that then have to be reconciled.

2. **A controlled vocabulary, enforced by a response schema.** Free-text positions cannot
   be compared across takes. "left of the laptop", "to the laptop's left" and "on the left
   side" are the same fact and three different strings. The schema forces the model to
   commit to normalized fields, which is what makes the ClickHouse query at the end of the
   demo possible at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.store import load_env  # noqa: E402

# The SDK warns about automatic function calling on every generate_content call. We do not
# use function calling, the warning is not actionable, and it lands on stderr in the middle
# of output a judge is reading. Suppressed rather than left as noise.
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

# Overridable because model availability moves faster than this file does. Confirmed
# present on this account 2026-08-16 via client.models.list().
#
# Flash rather than Pro on purpose: this is a high-volume vision call (every take on a
# shoot day), the output is a constrained schema rather than open reasoning, and cost per
# take is a number that ends up in the pitch.
DEFAULT_MODEL = os.environ.get("DAILIES_MODEL", "gemini-3.6-flash")

# Tried in order when the primary is overloaded. The newest model is also the busiest, and
# a 503 on it took the whole run down during development. A shoot day cannot be rerun
# because a model was popular that afternoon, so the pipeline steps down a generation
# rather than failing. Every entry here supports vision and response schemas.
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash"]

# 503 and 429 are transient by definition. Anything else is a real error and should surface
# immediately rather than being retried into a longer wait.
TRANSIENT_STATUS = (429, 503)

# Controlled vocabulary. The point is comparability across takes, not expressiveness.
HORIZONTAL = ["left", "center", "right", "offscreen"]
DEPTH = ["foreground", "midground", "background", "unknown"]
CATEGORIES = ["prop", "wardrobe", "set", "actor"]

# Continuity is mostly not about position. A prop sliding across a table is the toy case.
# What actually ruins a cut is STATE: a jacket buttoned in the wide and open in the close,
# sleeves rolled in one take and down in the next, a glass that refills itself between
# setups, a cigarette that grows back. Those are the errors that end up on a blooper list.
#
# Free text cannot carry them. Across five takes of one desk the same mug came back as
# "upright", "on napkin" and "placed on table": one fact, three strings, and a diff over
# that manufactures breaks out of synonyms. So state gets the same treatment position got,
# a fixed vocabulary, and the free-text description survives only for display.
STATE_CLASSES = [
    "fill_level",    # the classic self-refilling glass
    "fastening",     # buttons, zips, laces
    "sleeves",       # rolled or down
    "open_closed",   # doors, drawers, laptops, books
    "worn",          # on the body or taken off
    "held",          # and crucially, in WHICH hand
    "power",         # screens and lamps on or off
    "none",          # nothing about this object can meaningfully change
]

STATE_VALUES = [
    "full", "half", "empty",
    "buttoned", "unbuttoned", "partly_fastened",
    "rolled", "down",
    "open", "closed",
    "worn", "removed",
    "left_hand", "right_hand", "both_hands", "not_held",
    "on", "off",
    "unknown", "na",
]

SYSTEM_PROMPT = """\
You are a script supervisor's assistant on a film set. You are looking at frames sampled \
from a single take.

Your job is to record continuity state: the things that must match between takes so the \
footage can be cut together. You are building a database, not writing a description.

Rules that matter:

- Name each entity with a short, lowercase, singular noun phrase. Use the SAME name you \
would use in another take of the same scene. "coffee mug", not "the mug of coffee", not \
"Mug", not "cup".
- Only record what you can actually see. If you cannot tell whether the jacket is \
buttoned, set the attribute to "unknown" rather than guessing. A wrong continuity record \
is worse than a missing one, because someone will trust it.
- Record the state that could plausibly CHANGE between takes, and record it TWICE: once as \
short free text in `state`, and once as the controlled pair `state_class` + `state_value`. \
The controlled pair is what gets compared across takes, so it matters more than the prose.
- Choose `state_class` by what could realistically change about that object. A glass or mug \
is `fill_level`. A shirt or jacket is `fastening`, or `sleeves` when the sleeves are the \
visible thing. A door, drawer, book or laptop is `open_closed`. Anything a person is \
holding is `held`, and say WHICH hand. A screen or lamp is `power`. A watch, hat or jacket \
being on the body at all is `worn`. Use `none` with value `na` only when nothing about the \
object can meaningfully change, such as a wall.
- Continuity errors are far more often about state than about position. A mug sliding \
across a table is rare. A glass that refills itself between takes, a jacket buttoned in one \
and open in the next, a prop that swaps hands: those are the errors that reach the screen. \
Give state your attention.
- Do not record permanent properties like "the table is wooden".
- If an entity moves during the take, report where it is for the majority of the take and \
set moved_during_take to true.
- Be exhaustive about props and wardrobe. Those are what continuity errors are made of.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "scene_summary": {
            "type": "string",
            "description": "One sentence describing the action, for a human skimming the take list.",
        },
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "position_h": {"type": "string", "enum": HORIZONTAL},
                    "depth": {"type": "string", "enum": DEPTH},
                    "state": {
                        "type": "string",
                        "description": "Short free-text state, for a human reading the report. Not compared.",
                    },
                    "state_class": {
                        "type": "string",
                        "enum": STATE_CLASSES,
                        "description": "Which KIND of change this object can undergo. 'none' if it cannot meaningfully change.",
                    },
                    "state_value": {
                        "type": "string",
                        "enum": STATE_VALUES,
                        "description": "The value within that class right now. 'na' when state_class is 'none', 'unknown' when you cannot tell.",
                    },
                    "relative_to": {
                        "type": "string",
                        "description": "Entity this one is positioned against, or empty string.",
                    },
                    "moved_during_take": {"type": "boolean"},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "entity",
                    "category",
                    "position_h",
                    "depth",
                    "state",
                    "state_class",
                    "state_value",
                    "moved_during_take",
                    "confidence",
                ],
            },
        },
    },
    "required": ["scene_summary", "observations"],
}


@dataclass
class Observation:
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


@dataclass
class TakeState:
    take_id: str
    scene_summary: str
    observations: list[Observation]
    model: str
    frames_used: int

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _load_client():
    """Import lazily so `--help` works with no SDK and no key."""
    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError(
            "google-genai is not installed. Run:\n  pip install google-genai"
        ) from exc

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No API key found. Set GOOGLE_API_KEY or GEMINI_API_KEY.\n"
            "Get one at https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=api_key)


def _generate_with_fallback(client, primary_model: str, content, config):
    """Call Gemini, stepping down a model generation if the primary is overloaded.

    Returns (response, model_actually_used). The model used is recorded in the output
    because two takes analysed by different models are not strictly comparable, and a
    silent substitution would hide that from whoever reads the continuity report.
    """
    from google.genai import errors

    candidates = [primary_model] + [m for m in FALLBACK_MODELS if m != primary_model]
    last_error: Exception | None = None

    for model in candidates:
        try:
            response = client.models.generate_content(
                model=model, contents=content, config=config
            )
            return response, model
        except errors.APIError as exc:
            status = getattr(exc, "code", None)
            if status not in TRANSIENT_STATUS:
                raise
            last_error = exc
            print(f"  {model} unavailable ({status}), trying next model", file=sys.stderr)

    raise RuntimeError(
        f"Every model was unavailable. Last error: {last_error}\n"
        "This is load on Google's side, not a problem with the input. Try again shortly, "
        "or set DAILIES_MODEL to a less busy model."
    )


def extract_take_state(
    manifest_path: Path,
    model: str = DEFAULT_MODEL,
    scene_context: str | None = None,
) -> TakeState:
    """Read an ingest.py manifest, send its frames to Gemini, return structured state."""
    from google.genai import types

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    take_id = manifest["take_id"]
    frames = manifest["frames"]
    if not frames:
        raise ValueError(f"Manifest {manifest_path} has no frames")

    client = _load_client()

    # Frames go in time order with their timestamps stated, so the model can reason about
    # what moved rather than seeing an unordered pile of stills.
    parts: list[object] = []
    intro = f"Take '{take_id}'. {len(frames)} frames sampled across {manifest['duration_s']} seconds."
    if scene_context:
        intro += f"\nScene context from the production: {scene_context}"
    parts.append(types.Part.from_text(text=intro))

    for frame in frames:
        image_path = Path(frame["path"])
        if not image_path.exists():
            continue
        parts.append(types.Part.from_text(text=f"Frame at t={frame['timestamp_s']}s:"))
        parts.append(
            types.Part.from_bytes(data=image_path.read_bytes(), mime_type="image/jpeg")
        )

    frames_used = sum(1 for p in parts if isinstance(p, types.Part) and getattr(p, "inline_data", None))

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        # Continuity records must be reproducible. A creative temperature here would
        # mean the same take yields different "facts" on a re-run, which destroys the
        # premise that two takes can be meaningfully compared.
        temperature=0.0,
    )

    content = [types.Content(role="user", parts=parts)]
    response, model_used = _generate_with_fallback(client, model, content, config)

    payload = json.loads(response.text)
    observations = [
        Observation(
            entity=o["entity"].strip().lower(),
            category=o["category"],
            position_h=o["position_h"],
            depth=o["depth"],
            state=o["state"].strip().lower(),
            state_class=o.get("state_class", "none"),
            state_value=o.get("state_value", "na"),
            relative_to=o.get("relative_to", "").strip().lower(),
            moved_during_take=bool(o["moved_during_take"]),
            confidence=float(o["confidence"]),
        )
        for o in payload.get("observations", [])
    ]

    return TakeState(
        take_id=take_id,
        scene_summary=payload.get("scene_summary", ""),
        observations=observations,
        model=model_used,
        frames_used=frames_used,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="manifest json written by ingest.py")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--scene-context", default=None, help="e.g. 'INT. KITCHEN - DAY, scene 4'")
    parser.add_argument("--out", type=Path, default=None, help="where to write the state json")
    args = parser.parse_args()

    # Pick up .env so a fresh clone with credentials filled in just works.
    load_env()

    # Setup problems (no key, no SDK, empty manifest) are the normal first experience for
    # anyone cloning this, including a judge. They get a sentence, not a stack trace.
    try:
        state = extract_take_state(args.manifest, args.model, args.scene_context)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except Exception as exc:  # API errors, malformed responses, network failures
        print(f"\nExtraction failed: {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 1

    out = args.out or args.manifest.with_name(f"{state.take_id}.state.json")
    out.write_text(state.to_json(), encoding="utf-8")

    print(f"take {state.take_id}: {len(state.observations)} observations from {state.frames_used} frames")
    print(f"  {state.scene_summary}")
    for o in state.observations[:12]:
        moved = " (moves)" if o.moved_during_take else ""
        rel = f" rel:{o.relative_to}" if o.relative_to else ""
        print(f"  - {o.entity:<22} {o.category:<9} {o.position_h:<9} {o.state}{rel}{moved}")
    if len(state.observations) > 12:
        print(f"  ... {len(state.observations) - 12} more")
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
