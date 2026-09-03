"""Close the gap between "not mentioned" and "not there".

The problem this fixes, measured on five real takes of one desk. After vocabulary
reconciliation the comparison still reported `desk` missing from four takes, `mousepad`
missing from three, and `computer mouse` missing from one. The desk did not vanish. It is in
every frame. Gemini simply did not enumerate it every time, and `compare.py` read silence as
absence.

That inference is wrong and it is the noisiest remaining failure. An extraction pass is not
exhaustive, so the absence of a mention is weak evidence, while a continuity report needs
strong evidence before it tells a crew something moved.

So instead of guessing, the system goes back and looks. For every canonical entity a take
failed to mention, it re-queries that take's frames with one direct question: is this object
visible, yes or no. A "yes" becomes a real observation and the false alarm disappears. A "no"
is now genuine evidence of absence, earned by looking rather than assumed from silence.

Observations recovered this way are tagged `via: presence_check` so nothing is silently
invented and a human can audit exactly which records came from the second look.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.store import load_env  # noqa: E402

logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

DEFAULT_MODEL = "gemini-3.6-flash"
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash"]
TRANSIENT_STATUS = (429, 503)

HORIZONTAL = ["left", "center", "right", "offscreen"]
DEPTH = ["foreground", "midground", "background", "unknown"]

SYSTEM_PROMPT = """\
You are auditing whether specific objects appear in frames from one film take.

IMPORTANT: a careful pass has already examined these exact frames and did NOT record any of \
the objects you are being asked about. The most likely explanation for most of them is that \
they are genuinely not in this take. Your default answer is "no".

Only answer "yes" if you can point to a SPECIFIC frame and say where in it the object is. \
You must give the timestamp of that frame in seen_at_timestamp. If you cannot name the frame \
you saw it in, you did not see it, and the answer is "no".

Do not reason from expectation. If the scene contains a desk and a laptop, that is not \
evidence that a phone is present. If an object would plausibly be somewhere off-frame, that \
is "no", not "yes". If an object is a common item that usually sits on a desk, that is still \
"no" unless you can see it.

- "no": you looked and it is not visible in any frame. This is the expected answer.
- "yes": you can name the frame and describe where it is. Requires seen_at_timestamp.
- "unclear": the relevant area is occluded, dark or out of focus in every frame.

A false "yes" is the worst outcome here. It erases a real continuity difference by claiming \
an object was present when it was not, and it does so silently.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "visible": {"type": "string", "enum": ["yes", "no", "unclear"]},
                    "seen_at_timestamp": {
                        "type": "number",
                        "description": "Timestamp of the frame it is visible in. Required for 'yes'. Use -1 otherwise.",
                    },
                    "where_in_frame": {
                        "type": "string",
                        "description": "For 'yes', concretely where in that frame, so a human can check.",
                    },
                    "position_h": {"type": "string", "enum": HORIZONTAL},
                    "depth": {"type": "string", "enum": DEPTH},
                    "state": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "entity",
                    "visible",
                    "seen_at_timestamp",
                    "where_in_frame",
                    "position_h",
                    "depth",
                    "state",
                    "confidence",
                ],
            },
        }
    },
    "required": ["checks"],
}


def _client():
    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError("google-genai is not installed. Run: pip install google-genai") from exc
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("No API key. Set GOOGLE_API_KEY. https://aistudio.google.com/apikey")
    return genai.Client(api_key=api_key)


def _generate_with_fallback(client, primary, content, config):
    from google.genai import errors

    for model in [primary] + [m for m in FALLBACK_MODELS if m != primary]:
        try:
            return client.models.generate_content(model=model, contents=content, config=config), model
        except errors.APIError as exc:
            if getattr(exc, "code", None) not in TRANSIENT_STATUS:
                raise
            print(f"  {model} unavailable, trying next", file=sys.stderr)
    raise RuntimeError("Every model was unavailable. This is load on Google's side.")


def check_take(
    client, take_id: str, frames: list[dict], missing: list[str], model: str
) -> list[dict]:
    """Ask, for one take, which of the missing entities are actually visible."""
    from google.genai import types

    parts: list[object] = [
        types.Part.from_text(
            text=(
                f"Take '{take_id}'. Check whether these objects appear in the frames below:\n"
                + "\n".join(f"- {e}" for e in missing)
            )
        )
    ]
    for frame in frames:
        path = Path(frame["path"])
        if not path.exists():
            continue
        parts.append(types.Part.from_text(text=f"Frame at t={frame['timestamp_s']}s:"))
        parts.append(types.Part.from_bytes(data=path.read_bytes(), mime_type="image/jpeg"))

    response, _ = _generate_with_fallback(
        client,
        model,
        [types.Content(role="user", parts=parts)],
        types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            temperature=0.0,
        ),
    )
    return json.loads(response.text).get("checks", [])


def run(state_dir: Path, manifest_dir: Path, model: str = DEFAULT_MODEL) -> dict:
    states: dict[str, dict] = {}
    for path in sorted(state_dir.glob("*.state.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        states[data["take_id"]] = data
    if len(states) < 2:
        raise ValueError(f"Need at least two takes, found {len(states)} in {state_dir}")

    canonical: set[str] = set()
    for state in states.values():
        for obs in state.get("observations", []):
            canonical.add(obs["entity"])

    client = _client()
    summary = {"recovered": 0, "confirmed_absent": 0, "unclear": 0, "per_take": {}}

    for take_id, state in states.items():
        present = {o["entity"] for o in state.get("observations", [])}
        missing = sorted(canonical - present)
        if not missing:
            continue

        manifest_path = manifest_dir / f"{take_id}.json"
        if not manifest_path.exists():
            print(f"  {take_id}: no manifest at {manifest_path}, skipping", file=sys.stderr)
            continue
        frames = json.loads(manifest_path.read_text(encoding="utf-8")).get("frames", [])
        if not frames:
            continue

        checks = check_take(client, take_id, frames, missing, model)
        recovered, absent, unclear, rejected = [], [], [], []

        # A "yes" has to be anchored to a frame that exists. Without this gate the model
        # recovered a smartphone at 0.95 confidence from a take that provably contains no
        # phone, which silently erased the one real continuity difference in the scene.
        # Requiring a checkable timestamp turns a vague impression into a claim that can be
        # falsified, and claims that cannot be falsified do not get written to the database.
        frame_times = [float(f["timestamp_s"]) for f in frames]
        tolerance = 1.0

        for check in checks:
            entity = check.get("entity", "").strip().lower()
            if entity not in canonical:
                continue
            verdict = check.get("visible")

            if verdict == "yes":
                stamp = float(check.get("seen_at_timestamp", -1))
                anchored = any(abs(stamp - t) <= tolerance for t in frame_times)
                if not anchored:
                    rejected.append(entity)
                    verdict = "unclear"

            if verdict == "yes":
                state.setdefault("observations", []).append(
                    {
                        "entity": entity,
                        "category": "prop",
                        "position_h": check.get("position_h", "center"),
                        "depth": check.get("depth", "unknown"),
                        "state": (check.get("state") or "present").strip().lower(),
                        # A recovered observation is a sighting, not a state reading. The
                        # presence check answers "is it there", so claiming to know whether
                        # a jacket is buttoned would be inventing detail the pass never
                        # looked for. Unknown state is excluded from the diff by design.
                        "state_class": "none",
                        "state_value": "unknown",
                        "relative_to": "",
                        "moved_during_take": False,
                        "confidence": float(check.get("confidence", 0.5)),
                        # Auditable: this record came from the second look, not the first pass,
                        # and it names the frame it was seen in so a human can check it.
                        "via": "presence_check",
                        "seen_at_timestamp": float(check.get("seen_at_timestamp", -1)),
                        "where_in_frame": check.get("where_in_frame", ""),
                    }
                )
                recovered.append(entity)
            elif verdict == "no":
                absent.append(entity)
            else:
                unclear.append(entity)

        summary["recovered"] += len(recovered)
        summary["confirmed_absent"] += len(absent)
        summary["unclear"] += len(unclear)
        summary["rejected"] = summary.get("rejected", 0) + len(rejected)
        summary["per_take"][take_id] = {
            "checked": missing,
            "recovered": recovered,
            "confirmed_absent": absent,
            "unclear": unclear,
            "rejected_unanchored": rejected,
        }
        extra = f", rejected {len(rejected)} unanchored" if rejected else ""
        print(
            f"  {take_id}: checked {len(missing)}, recovered {len(recovered)}, "
            f"absent {len(absent)}, unclear {len(unclear)}{extra}"
        )

    for take_id, state in states.items():
        (state_dir / f"{take_id}.state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path, help="directory of reconciled *.state.json")
    parser.add_argument(
        "--manifests",
        type=Path,
        default=None,
        help="directory holding the ingest manifests (default: parent of state_dir)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    # Pick up .env so a fresh clone with credentials filled in just works.
    load_env()

    manifest_dir = args.manifests or args.state_dir.parent

    try:
        summary = run(args.state_dir, manifest_dir, args.model)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nPresence check failed: {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 1

    print(
        f"\nrecovered {summary['recovered']} observations that the first pass missed, "
        f"confirmed {summary['confirmed_absent']} genuinely absent, "
        f"{summary['unclear']} unclear"
    )
    print(f"now run: python pipeline/compare.py {args.state_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
