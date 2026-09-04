"""Resolve the same physical object appearing under different names across takes.

Why this exists, measured rather than assumed. Running five real 3-minute takes of one
desk, Gemini named the same monitor "monitor" in takes 1 and 3 and "computer monitor" in
takes 2, 4 and 5. Same for "mouse" versus "computer mouse". `compare.py` groups by exact
string, so it saw two entities where there was one, each apparently missing from the takes
that used the other name, and reported six continuity breaks that do not exist. One finding
out of eight was real. A tool that cries wolf six times out of seven is worse than no tool,
because a crew stops trusting it on the first day.

The fix is a reconciliation pass: look at every entity name the scene produced, decide which
names denote the same object, and rewrite the observations to a single canonical vocabulary
before anything is compared.

The dangerous direction is over-merging. A false merge hides a REAL continuity break, which
is the failure this whole product exists to prevent, and it is silent. A false split merely
produces noise someone can dismiss. So the prompt is deliberately biased toward leaving
names separate when there is any doubt, and every merge is recorded so a human can audit it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.store import load_env  # noqa: E402

logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

DEFAULT_MODEL = "gemini-3.6-flash"
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash"]
TRANSIENT_STATUS = (429, 503)

SYSTEM_PROMPT = """\
You are reconciling the vocabulary used across several takes of a single film scene.

Each take was described independently, so the same physical object may have been given \
slightly different names. Your job is to decide which names refer to the SAME physical \
object and choose one canonical name for each group.

Rules, in priority order:

1. Merge ONLY when the names denote the same physical object in the same scene. \
"mouse" and "computer mouse" are the same object. "monitor" and "computer monitor" are the \
same object.
2. NEVER merge distinct objects, even when they are related or share a category. A laptop \
and a monitor are different. Headphones and a microphone are different. A mug and a napkin \
are different. A mousepad and a mouse are different.
3. When in doubt, DO NOT MERGE. Leaving two names separate produces noise a human can \
dismiss. Merging two different objects hides a real continuity error, which is the exact \
failure this system exists to prevent, and nobody will notice it happened.
4. For the canonical name, pick the clearest and most specific name actually used. Do not \
invent a name that no take produced.
5. A name that appears in only one take is usually just an object that was only visible in \
that take. That is a real observation, not a naming problem. Leave it alone unless it is \
plainly the same object as another name.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "canonical": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "reasoning": {"type": "string"},
                },
                "required": ["canonical", "aliases", "reasoning"],
            },
        }
    },
    "required": ["groups"],
}


def load_states(state_dir: Path) -> dict[str, dict]:
    states: dict[str, dict] = {}
    for path in sorted(state_dir.glob("*.state.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        states[data["take_id"]] = data
    return states


def collect_entities(states: dict[str, dict]) -> dict[str, dict]:
    """Every distinct entity name, with the takes it appeared in and how it was described.

    The category and state text go to the model as evidence: "monitor (prop, off)" and
    "computer monitor (set, powered off)" are recognisably one object, and that context is
    what makes the judgement safe rather than a guess based on string similarity.
    """
    info: dict[str, dict] = defaultdict(lambda: {"takes": set(), "categories": set(), "states": set()})
    for take_id, state in states.items():
        for obs in state.get("observations", []):
            entry = info[obs["entity"]]
            entry["takes"].add(take_id)
            entry["categories"].add(obs.get("category", ""))
            entry["states"].add(obs.get("state", ""))
    return {
        name: {
            "takes": sorted(v["takes"]),
            "categories": sorted(c for c in v["categories"] if c),
            "states": sorted(s for s in v["states"] if s),
        }
        for name, v in info.items()
    }


def _client():
    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError("google-genai is not installed. Run: pip install google-genai") from exc

    import os

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("No API key. Set GOOGLE_API_KEY. https://aistudio.google.com/apikey")
    from pipeline.client import make_client

    return make_client()


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


def reconcile_entities(entities: dict[str, dict], model: str = DEFAULT_MODEL) -> tuple[dict[str, str], list[dict]]:
    """Return (alias -> canonical mapping, the groups as reasoned by the model)."""
    from google.genai import types

    if len(entities) < 2:
        return {}, []

    lines = []
    for name, meta in sorted(entities.items()):
        cats = "/".join(meta["categories"]) or "?"
        states = "; ".join(meta["states"][:3]) or "?"
        lines.append(f'- "{name}" ({cats}) seen in {len(meta["takes"])} take(s): {states}')

    prompt = (
        "These entity names were produced independently while describing takes of one scene.\n"
        "Group the names that refer to the same physical object.\n\n" + "\n".join(lines)
    )

    client = _client()
    response, _ = _generate_with_fallback(
        client,
        model,
        [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
        types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            temperature=0.0,
        ),
    )

    payload = json.loads(response.text)
    mapping: dict[str, str] = {}
    groups = []
    known = set(entities.keys())

    for group in payload.get("groups", []):
        canonical = group.get("canonical", "").strip().lower()
        aliases = [a.strip().lower() for a in group.get("aliases", [])]
        # Only accept names the takes actually produced. A model that invents a canonical
        # name would rewrite observations into something no take ever saw.
        aliases = [a for a in aliases if a in known]
        if canonical not in known:
            if not aliases:
                continue
            canonical = aliases[0]
        if len(aliases) < 2:
            continue  # a group of one changes nothing
        for alias in aliases:
            mapping[alias] = canonical
        groups.append({"canonical": canonical, "aliases": aliases, "reasoning": group.get("reasoning", "")})

    return mapping, groups


def apply_mapping(states: dict[str, dict], mapping: dict[str, str]) -> dict[str, dict]:
    """Rewrite entity names, collapsing duplicates that merging may create within a take."""
    out: dict[str, dict] = {}
    for take_id, state in states.items():
        new_state = json.loads(json.dumps(state))  # deep copy, plain data only
        merged: dict[str, dict] = {}
        for obs in new_state.get("observations", []):
            obs["entity"] = mapping.get(obs["entity"], obs["entity"])
            existing = merged.get(obs["entity"])
            # If a merge puts two observations of one object in the same take, keep the more
            # confident reading rather than letting arbitrary order decide.
            if existing is None or float(obs.get("confidence", 0)) > float(existing.get("confidence", 0)):
                merged[obs["entity"]] = obs
        new_state["observations"] = list(merged.values())
        out[take_id] = new_state
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path, help="directory of *.state.json for one scene")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out", type=Path, default=None, help="default: <state_dir>/reconciled")
    args = parser.parse_args()

    # Pick up .env so a fresh clone with credentials filled in just works.
    load_env()

    try:
        states = load_states(args.state_dir)
        if len(states) < 2:
            print(f"Need at least two takes, found {len(states)} in {args.state_dir}", file=sys.stderr)
            return 1

        entities = collect_entities(states)
        mapping, groups = reconcile_entities(entities, args.model)
    except (RuntimeError, ValueError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nReconciliation failed: {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 1

    reconciled = apply_mapping(states, mapping)
    out_dir = args.out or (args.state_dir / "reconciled")
    out_dir.mkdir(parents=True, exist_ok=True)
    for take_id, state in reconciled.items():
        (out_dir / f"{take_id}.state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    print(f"{len(entities)} distinct entity names across {len(states)} takes")
    if groups:
        print(f"{len(groups)} merged into one object each:")
        for g in groups:
            others = [a for a in g["aliases"] if a != g["canonical"]]
            print(f"  '{g['canonical']}'  <-  {', '.join(repr(o) for o in others)}")
            print(f"      {g['reasoning']}")
    else:
        print("no names needed merging")
    print(f"\nwrote {len(reconciled)} reconciled takes to {out_dir}")
    print(f"now run: python pipeline/compare.py {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
