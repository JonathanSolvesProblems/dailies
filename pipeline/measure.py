"""Measure what a shoot day actually costs to run through Dailies.

The point of this file is WHO GRADES THE NUMBER.

Any accuracy figure this project produces is my code scored against ground truth I set on
footage I shot. That is a demo, not a result. Cost is different: the token counts come from
Google's own API response, and the price per token comes from Google's published rate card.
Neither half is mine to influence. The comparison it feeds is the same: a shoot day rate
from a published industry survey, not a figure I chose.

So this reports one thing, in dollars, that someone can check without trusting me.

    python pipeline/measure.py --scene out/scene_a
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.store import load_env  # noqa: E402

# Google's published paid-tier rates, USD per 1M tokens, checked against the source below.
#
# Copied by hand and got them wrong the first time: I put 2.5 Flash's $0.30/$2.50 against
# every model, which understated 3.6 Flash by 2.5x and 3.5 Flash by 5x. The rates are not
# monotonic with the version number and cannot be guessed. Re-read the page before quoting
# any figure this file produces, because a stale rate card turns an externally graded number
# back into an invented one, which is the entire failure this file exists to avoid.
#
# Vision input is billed at the text rate on all of these, so frames cost the same per token
# as prompt text.
PRICING = {
    # Promotional through 2026-12-31. Rises to $1.50 / $7.50 on 2027-01-01.
    "gemini-3.6-flash":      {"input": 0.75, "output": 3.75},
    "gemini-3.5-flash":      {"input": 1.50, "output": 9.00},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    "gemini-2.5-flash":      {"input": 0.30, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
}
PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
PRICING_CHECKED = "2026-09-03"
PRICING_NOTE = "3.6 Flash is promotional until 2027-01-01, when it doubles to $1.50/$7.50"


@dataclass
class Usage:
    label: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int

    @property
    def cost_usd(self) -> float:
        # Deliberately raises on an unknown model rather than falling back to a neighbour's
        # rate. Rates differ 15x across this family, so a silent substitution would print a
        # confident dollar figure that is simply wrong, which is worse than no figure.
        if self.model not in PRICING:
            raise KeyError(
                f"No published rate on file for '{self.model}'. Add it from {PRICING_SOURCE} "
                "rather than guessing."
            )
        rate = PRICING[self.model]
        return (
            self.input_tokens / 1_000_000 * rate["input"]
            + self.output_tokens / 1_000_000 * rate["output"]
        )


@dataclass
class Report:
    runs: list[Usage] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.runs)

    @property
    def total_ms(self) -> int:
        return sum(r.latency_ms for r in self.runs)


def measure_extraction(manifest: Path, scene_context: str | None = None, attempts: int = 4) -> Usage:
    """One real extraction call, with the token counts the API itself reports.

    Retries on 503. The first run of this measurement lost two takes of three to "model is
    currently experiencing high traffic" and reported a cost from a single sample, which is
    an anecdote rather than a measurement. The production path already retries and falls
    back; a measurement that does not is measuring a different system than the one that
    ships.
    """
    from google import genai
    from google.genai import types

    import os

    from pipeline.extract import (
        DEFAULT_MODEL,
        RESPONSE_SCHEMA,
        SYSTEM_PROMPT,
    )

    data = json.loads(manifest.read_text(encoding="utf-8"))
    frames = data.get("frames", [])
    if not frames:
        raise ValueError(f"{manifest} has no frames")

    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    parts: list = [
        types.Part.from_text(
            text=f"Take '{data['take_id']}'. {len(frames)} frames across {data['duration_s']}s."
            + (f"\nScene: {scene_context}" if scene_context else "")
        )
    ]
    for frame in frames:
        path = Path(frame["path"])
        if path.exists():
            parts.append(types.Part.from_text(text=f"Frame at t={frame['timestamp_s']}s:"))
            parts.append(types.Part.from_bytes(data=path.read_bytes(), mime_type="image/jpeg"))

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        temperature=0.0,
    )

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            started = time.perf_counter()
            response = client.models.generate_content(
                model=DEFAULT_MODEL,
                contents=[types.Content(role="user", parts=parts)],
                config=config,
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            meta = response.usage_metadata
            return Usage(
                label=f"extract {data['take_id']}",
                model=DEFAULT_MODEL,
                input_tokens=meta.prompt_token_count or 0,
                output_tokens=(meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0),
                latency_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001 - retry on anything transient
            last = exc
            if "503" not in str(exc) and "UNAVAILABLE" not in str(exc):
                raise
            if attempt < attempts - 1:
                backoff = 2 ** attempt * 5
                print(f"    503, retrying in {backoff}s ({attempt + 1}/{attempts - 1})")
                time.sleep(backoff)
    raise last  # type: ignore[misc]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, default=ROOT / "out" / "scene_a")
    parser.add_argument("--takes", type=int, default=3, help="how many takes to measure")
    parser.add_argument(
        "--shoot-day-usd",
        type=float,
        default=1440.0,
        help="published cost of one shoot day, for the comparison",
    )
    args = parser.parse_args()

    load_env()
    manifests = sorted(p for p in args.scene.glob("take_*.json") if not p.name.endswith(".state.json"))
    if not manifests:
        print(f"No take manifests under {args.scene}", file=sys.stderr)
        return 1
    manifests = manifests[: args.takes]

    report = Report()
    print(f"measuring {len(manifests)} real extraction calls\n")
    for manifest in manifests:
        try:
            usage = measure_extraction(manifest)
        except Exception as exc:
            print(f"  {manifest.name}: FAILED {type(exc).__name__}: {str(exc)[:90]}", file=sys.stderr)
            continue
        report.runs.append(usage)
        print(
            f"  {usage.label:<22} {usage.input_tokens:>7} in  {usage.output_tokens:>6} out  "
            f"{usage.latency_ms:>6}ms  ${usage.cost_usd:.5f}"
        )

    if not report.runs:
        return 1

    per_take = report.total_cost / len(report.runs)
    per_take_ms = report.total_ms / len(report.runs)

    # A shoot day is not five takes. Scale to something a production would recognise: a
    # modest day is on the order of 40 setups.
    takes_per_day = 40
    day_cost = per_take * takes_per_day
    day_minutes = (per_take_ms * takes_per_day) / 60000

    print(f"\n  per take        ${per_take:.5f}   {per_take_ms/1000:.1f}s")
    print(f"  x{takes_per_day} takes/day   ${day_cost:.2f}   {day_minutes:.0f} min of compute")
    print(f"\n  a shoot day costs ${args.shoot_day_usd:,.0f} (published survey, 6-person crew)")
    print(f"  Dailies processes that day for ${day_cost:.2f}, or {day_cost / args.shoot_day_usd * 100:.3f}% of it")
    print(f"\n  token counts: reported by the Gemini API")
    print(f"  prices:       {PRICING_SOURCE} (checked {PRICING_CHECKED})")
    print(f"  day rate:     published industry survey, not chosen here")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
