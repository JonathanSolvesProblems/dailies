"""Catch a continuity break while the camera is still rolling.

The batch pipeline answers "what went wrong today". This answers "what is wrong right
now", which is the question the job actually asks. A script supervisor who finds the mug
on the wrong side at wrap has documented a reshoot; one who finds it in the first twenty
seconds of the take has prevented it.

The shape is deliberately different from `extract.py`. That describes a take from scratch
and is thorough and slow. This is handed a reference take's state and asked one narrow
question: does what I am looking at right now still match? A closed question against a
known answer is both faster and far more accurate than an open one, and it is the only
version that fits inside the seconds a take gives you.

One frame, one call, no history. Statelessness is the point: a live check that depends on
having seen the previous frame cannot recover from a dropped connection on a set, and sets
drop connections.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, asdict, field

logging.getLogger("google_genai").setLevel(logging.ERROR)

# Flash, not flash-lite, and the reason is worth keeping.
#
# Lite benchmarked about ten times faster on this exact call: 979ms median against
# 5-15s. It was chosen on that basis, and then an adversarial test killed it. Given a
# horizontally mirrored frame, in which every object on the desk is on the wrong side,
# lite reported "laptop, monitor, coffee mug, and microphone are in their expected
# positions". Not low confidence. Not a partial catch. It did not see a fully mirrored
# room. Flash, on the same frame, flagged the mug, microphone, mouse and mousepad at 0.95
# each and named the swap.
#
# The speed was not a tradeoff, it was an artefact of not looking. A detector with no
# recall is not a fast detector, it is a green light wired to nothing, and on a set that
# is worse than no tool at all because someone will trust it.
#
# So: flash, and the check interval absorbs the latency instead. A take runs well over
# thirty seconds, so a check every few seconds still catches a break inside the take,
# which is the whole requirement.
DEFAULT_MODEL = os.environ.get("DAILIES_LIVE_MODEL", "gemini-3.6-flash")
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash"]

# Clients send frames at roughly this width. Glasses capture at 1552x2064, which is far
# more pixels than "did the mug move" needs and every one of them is latency on the wire
# and in the model. Downscaling is done at the client so it also saves the upload.
TARGET_FRAME_WIDTH = 512

# Below this the model is guessing, and a false call mid-take costs a director's trust
# immediately. Live tolerates missing a subtle break far better than crying wolf.
MIN_CONFIDENCE = 0.65

SYSTEM_PROMPT = """\
You are a script supervisor's live assistant. A take is rolling right now.

You will be given the continuity state recorded from a REFERENCE take, and one frame from
the take currently being shot. Your only job is to report whether anything visible in this
frame contradicts the reference.

Rules:

- Report a divergence ONLY for something you can see in this frame. If an object from the
  reference is simply not visible in this crop or angle, that is NOT a divergence. The
  camera moves; the objects have not necessarily moved with it.
- Position is judged relative to the other objects in frame, not to the frame edges,
  because the wearer's head moves constantly and the frame edges move with it.
- Ignore the wearer's own body: hands, sleeves, and anything held by the person wearing the
  camera are not set continuity.
- Ignore lighting, focus, motion blur and exposure. Those are not continuity.
- Be conservative. Interrupting a take is expensive. Report only what you would be willing
  to stop the camera for, and set confidence honestly.
- If nothing contradicts the reference, return an empty list. That is the expected answer
  and it is a good answer.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "divergences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "expected": {"type": "string", "description": "what the reference take recorded"},
                    "observed": {"type": "string", "description": "what is visible in this frame"},
                    "confidence": {"type": "number"},
                },
                "required": ["entity", "expected", "observed", "confidence"],
            },
        },
        "frame_note": {
            "type": "string",
            "description": "One short clause on what is visible, for the operator's log.",
        },
    },
    "required": ["divergences", "frame_note"],
}


@dataclass
class Divergence:
    entity: str
    expected: str
    observed: str
    confidence: float


@dataclass
class LiveCheck:
    ok: bool
    divergences: list[Divergence] = field(default_factory=list)
    frame_note: str = ""
    model: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "divergences": [asdict(d) for d in self.divergences],
            "frame_note": self.frame_note,
            "model": self.model,
            "latency_ms": self.latency_ms,
        }


def reference_summary(observations: list[dict]) -> str:
    """Flatten a reference take's state into the smallest thing worth sending.

    Only the comparable fields go. Free-text state is left out on purpose: across takes of
    an identical scene the same mug was described 'upright', 'on napkin' and 'placed on
    table', and feeding that back as an expectation manufactures divergences out of
    synonyms, which is the exact failure the batch pipeline had to be fixed for.
    """
    lines = []
    for obs in observations:
        if obs.get("category") == "actor":
            continue
        rel = f", by the {obs['relative_to']}" if obs.get("relative_to") else ""
        lines.append(f"- {obs['entity']}: {obs.get('position_h', '?')}{rel}")
    return "\n".join(lines)


async def check_frame_async(
    frame_bytes: bytes,
    observations: list[dict],
    scene_context: str | None = None,
    model: str = DEFAULT_MODEL,
) -> LiveCheck:
    import time

    from google import genai
    from google.genai import types

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("No API key. Set GOOGLE_API_KEY.")
    if not observations:
        raise ValueError("No reference state. Process a reference take first.")

    client = genai.Client(api_key=api_key)

    prompt = "REFERENCE TAKE recorded this:\n" + reference_summary(observations)
    if scene_context:
        prompt += f"\n\nScene: {scene_context}"
    prompt += "\n\nThe frame below is from the take rolling now. What contradicts the reference?"

    parts = [
        types.Part.from_text(text=prompt),
        types.Part.from_bytes(data=frame_bytes, mime_type="image/jpeg"),
    ]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        temperature=0.0,
    )

    started = time.perf_counter()
    last_error: Exception | None = None

    for candidate in [model] + [m for m in FALLBACK_MODELS if m != model]:
        try:
            response = await client.aio.models.generate_content(
                model=candidate,
                contents=[types.Content(role="user", parts=parts)],
                config=config,
            )
            payload = json.loads(response.text)
            divergences = [
                Divergence(
                    entity=d["entity"].strip().lower(),
                    expected=str(d["expected"]).strip(),
                    observed=str(d["observed"]).strip(),
                    confidence=float(d["confidence"]),
                )
                for d in payload.get("divergences", [])
                if float(d.get("confidence", 0)) >= MIN_CONFIDENCE
            ]
            return LiveCheck(
                ok=not divergences,
                divergences=divergences,
                frame_note=payload.get("frame_note", ""),
                model=candidate,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            if "503" not in str(exc) and "429" not in str(exc):
                raise
            last_error = exc

    raise RuntimeError(f"Every model was unavailable. Last error: {last_error}")


def decode_frame(data: str) -> bytes:
    """Accept a bare base64 payload or a data: URL, because clients send both."""
    if data.startswith("data:"):
        _, _, data = data.partition(",")
    return base64.b64decode(data)
