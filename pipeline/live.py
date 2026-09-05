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

# Flash-Lite is disqualified on RECALL, not speed, and that is worth keeping in front of
# whoever next tries to make this faster.
#
# Lite benchmarked about ten times faster on this exact call: 979ms against 5-15s. It was
# chosen on that basis, then an adversarial test killed it. Given a horizontally mirrored
# frame, every object on the desk on the wrong side, lite reported "laptop, monitor, coffee
# mug, and microphone are in their expected positions". Not low confidence, not a partial
# catch: it did not see a fully mirrored room. Flash flagged four objects at 0.95 and named
# the swap. The speed was an artefact of not looking, and a detector with no recall is a
# green light wired to nothing.
#
# 3.5, not 3.6, and not lite. Recall and latency measured together, 3 runs per side,
# control frame (must stay silent) and mirrored frame (must flag):
#
#   3.6-flash  512px   11263ms   0/3 false alarm   3/3 caught
#   3.6-flash  384px   24268ms   0/3 false alarm   3/3 caught
#   3.5-flash  512px    3678ms   0/3 false alarm   3/3 caught
#   3.5-flash  384px    3902ms   0/3 false alarm   3/3 caught
#
# 3.5 is three times faster than 3.6 at identical recall: the newer model is slower here
# without being better at the task. Frame size barely moves it, so the lever is the model,
# not the pixels. Note that 3.6 at 384px was SLOWER than at 512px, which is server-side
# variance rather than anything about the image, and is a good reason to measure a
# configuration rather than reason about it.
#
# Lite is still excluded and always will be. See the note below.
DEFAULT_MODEL = os.environ.get("DAILIES_LIVE_MODEL", "gemini-3.5-flash")
FALLBACK_MODELS = ["gemini-3.6-flash", "gemini-2.5-flash"]

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
- STATE matters more than position. A prop sliding across a table is the rare error. What
  actually ruins a cut is a glass at a different level, a jacket buttoned in one take and
  open in the next, sleeves rolled then down, a door that changed, or a prop that swapped
  hands. Check those first and hardest.
- Wardrobe ON THE PERSON BEING FILMED is continuity and must be reported. Wardrobe on the
  person WEARING the camera is not: their own hands and sleeves drift through frame
  constantly and are not the scene.
- `held` is the one reference field that is a snapshot rather than a settled fact. The
  reference records the take as a whole, but an actor picks a prop up and puts it down many
  times inside one take, so at any given instant it may simply be resting. Report a `held`
  divergence ONLY when the prop is in a DIFFERENT HAND than the reference says. A prop the
  reference lists as held that is currently sitting on the table is NOT a divergence, and
  neither is the reverse. The error that ruins a cut is the phone that changed hands between
  takes, never the hand being momentarily empty.
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

    Only the controlled fields go. Free-text state is left out on purpose: across takes of
    an identical scene the same mug was described 'upright', 'on napkin' and 'placed on
    table', and feeding that back as an expectation manufactures divergences out of
    synonyms, which is the exact failure the batch pipeline had to be fixed for.

    The controlled state pair DOES go, because it is the half that matters. Telling the
    model only where things were would leave it unable to see the errors that actually
    reach the screen: the glass that refilled, the jacket that came unbuttoned, the prop
    that changed hands.
    """
    lines = []
    for obs in observations:
        if obs.get("category") == "actor":
            continue
        rel = f", by the {obs['relative_to']}" if obs.get("relative_to") else ""
        state_class = obs.get("state_class", "none")
        state_value = obs.get("state_value", "na")
        state = ""
        if state_class not in ("none", "", None) and state_value not in ("na", "unknown", "", None):
            state = f", {state_class}={state_value}"
        lines.append(f"- {obs['entity']}: {obs.get('position_h', '?')}{rel}{state}")
    return "\n".join(lines)


def build_prompt(observations: list[dict], scene_context: str | None = None) -> str:
    """The exact user turn sent with the frame.

    A function rather than three lines inlined in the caller, because a benchmark that
    retypes the prompt is not measuring this system. Turning thinking off was justified on a
    hand-rebuilt prompt that differed from this one by its closing sentence; against the real
    text, the same setting caught 0 of 3 mirrored frames instead of 3 of 3, and shipped.

    Any harness, test or experiment must import this. Never retype it.
    """
    prompt = "REFERENCE TAKE recorded this:\n" + reference_summary(observations)
    if scene_context:
        prompt += f"\n\nScene: {scene_context}"
    prompt += "\n\nThe frame below is from the take rolling now. What contradicts the reference?"
    return prompt


async def check_frame_async(
    frame_bytes: bytes,
    observations: list[dict],
    scene_context: str | None = None,
    model: str = DEFAULT_MODEL,
) -> LiveCheck:
    import time

    from google import genai
    from google.genai import types

    if not observations:
        raise ValueError("No reference state. Process a reference take first.")

    from pipeline.client import make_client

    client = make_client()

    prompt = build_prompt(observations, scene_context)

    parts = [
        types.Part.from_text(text=prompt),
        types.Part.from_bytes(data=frame_bytes, mime_type="image/jpeg"),
    ]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        temperature=0.0,
        # THINKING STAYS ON. Do not turn it off again without re-reading this.
        #
        # It was set to thinking_budget=0 for one day on a measurement that looked careful and
        # was worthless: I rebuilt the prompt by hand in the test harness instead of importing
        # the one this function sends. The two differ by a single trailing sentence, and that
        # sentence decides everything.
        #
        # Measured properly, three runs per cell, control frame and horizontally mirrored frame:
        #
        #   PRODUCTION prompt, thinking on   mirrored 3/3 caught   control 0/3 false alarm
        #   PRODUCTION prompt, budget 0      mirrored 0/3 caught   control 0/3 false alarm
        #   my harness prompt, thinking on   mirrored 3/3 caught   control 0/3 false alarm
        #   my harness prompt, budget 0      mirrored 3/3 caught   control 0/3 false alarm
        #
        # With the prompt this code actually sends, thinking off is completely blind to a room
        # where every object has swapped sides, and silent about it. It is the Flash-Lite
        # result exactly: 27% faster because it had stopped looking. The negative control still
        # passed 6/6, which is why this survived a day undetected. A quiet detector looks
        # identical to a working one until something is wrong in front of it.
        #
        # The lesson is not about thinking budgets. It is that a recall test must exercise the
        # code path that ships, not a reconstruction of it. Import the prompt; never retype it.
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
            result = LiveCheck(
                ok=not divergences,
                divergences=divergences,
                frame_note=payload.get("frame_note", ""),
                model=candidate,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            # Fire-and-forget. A crew is waiting on this answer; bookkeeping does not get
            # to slow it down or break it.
            try:
                from pipeline.telemetry import Run, record
                record(Run(
                    operation="live_check",
                    model=candidate,
                    latency_ms=result.latency_ms,
                    outcome="divergence" if divergences else "holds",
                    findings=len(divergences),
                    entities=[d.entity for d in divergences],
                    detail=result.frame_note,
                ))
            except Exception:
                pass
            return result
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
