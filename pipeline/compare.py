"""Find what changed between takes of the same scene.

This is the payoff. Everything upstream exists so that this comparison is a set operation
over normalized fields instead of a human reading four paragraphs.

A note on what is comparable, learned by measuring rather than assuming. Running the same
scene four times, Gemini named the entity "coffee mug" every time and reported position as
the enum values left / center / right. Those compare cleanly. It described the same
situation as "upright", "on table" and "placed on table" in the free-text state field.
Those do not compare, and pretending they do would manufacture continuity breaks that are
really just synonyms.

So the diff runs on the normalized fields only (presence, position_h, depth, state_value)
and carries the free-text state along for display. A field that cannot be compared reliably
is worse than no field, because a false continuity flag costs a crew real time chasing
nothing.

`state_value` was added to that list once it became an enum. It is the field that matters
most: a prop sliding across a table is the rare case, while a jacket buttoned in one take
and open in the next, a glass that refills itself, or a prop that swaps hands, are the
errors that actually reach the screen.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

# Fields whose values come from a fixed vocabulary and can therefore be compared across
# takes. Anything not in here is descriptive only.
#
# state_value earns its place here now that it is an enum rather than prose. It is also the
# field that matters most: continuity errors are far more often a jacket coming unbuttoned
# or a glass refilling itself than a prop sliding across a table. The free-text `state`
# stays out, because "upright" / "on napkin" / "placed on table" were one fact in three
# strings and comparing them manufactured breaks that did not exist.
COMPARABLE_FIELDS = ("position_h", "depth", "state_value")

# Below this, the model was unsure enough that a mismatch is more likely to be a bad read
# than a real continuity break.
MIN_CONFIDENCE = 0.4

# When this many entities all shift the same field in the same take, the wearer moved, not
# the props.
#
# Measured: in take_005 of the desk scene, `desk`, `computer mouse` and `mousepad` all went
# foreground -> midground together. Nothing on that desk moved. The camera was further back,
# and from head-mounted POV the whole frame reparallaxes at once. Reporting that as three
# continuity breaks is worse than useless, because it buries the two real findings in the
# same list.
#
# A prop moving is a local event. A camera moving is a global one. Counting how many entities
# moved together is what separates them, and it costs nothing.
CAMERA_SHIFT_THRESHOLD = 3


@dataclass
class Delta:
    """One continuity difference: an entity whose state does not match the other takes."""

    entity: str
    field: str
    expected: str
    outliers: dict[str, str]  # take_id -> the value it had instead
    agreeing_takes: list[str]

    @property
    def severity(self) -> str:
        """A break in one take out of many is the classic continuity error.

        A field that is all over the place is more likely a shaky read than a real problem,
        so it gets flagged lower rather than shouted about.
        """
        if len(self.outliers) == 1 and len(self.agreeing_takes) >= 2:
            return "likely break"
        if len(self.agreeing_takes) >= len(self.outliers):
            return "possible break"
        return "unstable reading"


@dataclass
class CameraShift:
    """The wearer moved, not the props. Reported once instead of once per object."""

    take_id: str
    field: str
    from_value: str
    to_value: str
    entities: list[str]


@dataclass
class MissingEntity:
    """An entity present in some takes of a scene and absent from others."""

    entity: str
    present_in: list[str]
    absent_from: list[str]


def load_states(state_files: list[Path]) -> dict[str, dict]:
    states = {}
    for path in state_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        states[data["take_id"]] = data
    return states


def compare(states: dict[str, dict]) -> tuple[list[Delta], list[MissingEntity]]:
    take_ids = sorted(states.keys())

    # entity -> take_id -> observation
    by_entity: dict[str, dict[str, dict]] = defaultdict(dict)
    for take_id, state in states.items():
        for obs in state.get("observations", []):
            if float(obs.get("confidence", 1.0)) < MIN_CONFIDENCE:
                continue
            # Keep the more confident reading if an entity somehow appears twice.
            existing = by_entity[obs["entity"]].get(take_id)
            if existing and float(existing.get("confidence", 0)) >= float(obs.get("confidence", 0)):
                continue
            by_entity[obs["entity"]][take_id] = obs

    deltas: list[Delta] = []
    missing: list[MissingEntity] = []

    for entity, per_take in sorted(by_entity.items()):
        present = sorted(per_take.keys())
        absent = [t for t in take_ids if t not in per_take]

        # An entity in some takes and not others is the loudest continuity signal there is,
        # so it is reported on its own rather than buried as a field mismatch.
        if absent and present:
            missing.append(MissingEntity(entity=entity, present_in=present, absent_from=absent))

        if len(present) < 2:
            continue

        for field in COMPARABLE_FIELDS:
            values = {take: per_take[take].get(field, "") for take in present}
            distinct = set(values.values())
            if len(distinct) <= 1:
                continue

            counts = Counter(values.values())
            expected, _ = counts.most_common(1)[0]
            outliers = {take: val for take, val in values.items() if val != expected}
            agreeing = [take for take, val in values.items() if val == expected]

            deltas.append(
                Delta(
                    entity=entity,
                    field=field,
                    expected=expected,
                    outliers=outliers,
                    agreeing_takes=sorted(agreeing),
                )
            )

    deltas, shifts = _split_camera_shifts(deltas)

    # Loudest first: a single outlier against a clear majority is what a crew wants to see.
    order = {"likely break": 0, "possible break": 1, "unstable reading": 2}
    deltas.sort(key=lambda d: (order[d.severity], d.entity))
    return deltas, missing, shifts


def _split_camera_shifts(deltas: list[Delta]) -> tuple[list[Delta], list["CameraShift"]]:
    """Separate whole-frame camera moves from individual props moving.

    Several entities changing the same field in the same take, all together, is the wearer
    having moved. It is one fact about the shot, not N facts about the props.
    """
    grouped: dict[tuple[str, str], list[Delta]] = defaultdict(list)
    for delta in deltas:
        for take in delta.outliers:
            grouped[(take, delta.field)].append(delta)

    shifted: set[int] = set()
    shifts: list[CameraShift] = []

    for (take, field), members in grouped.items():
        if len(members) < CAMERA_SHIFT_THRESHOLD:
            continue
        # Only a shift if they all moved to the SAME value; scattered values are noise.
        values = {d.outliers[take] for d in members}
        if len(values) != 1:
            continue
        shifts.append(
            CameraShift(
                take_id=take,
                field=field,
                from_value=members[0].expected,
                to_value=next(iter(values)),
                entities=sorted(d.entity for d in members),
            )
        )
        shifted.update(id(d) for d in members)

    remaining = [d for d in deltas if id(d) not in shifted]
    return remaining, shifts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path, help="directory of *.state.json files for one scene")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    files = sorted(args.state_dir.glob("*.state.json"))
    if len(files) < 2:
        print(f"Need at least two takes to compare, found {len(files)} in {args.state_dir}")
        return 1

    states = load_states(files)
    deltas, missing, shifts = compare(states)

    if args.json:
        print(
            json.dumps(
                {
                    "takes": sorted(states.keys()),
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
                },
                indent=2,
            )
        )
        return 0

    print(f"\nScene: {len(states)} takes ({', '.join(sorted(states.keys()))})\n")

    if not deltas and not missing and not shifts:
        print("  Continuity consistent across all takes.")
        return 0

    for s in shifts:
        print(f"  [{'camera move':<16}] {s.take_id}: {len(s.entities)} objects shifted {s.field} together")
        print(f"       {s.from_value} -> {s.to_value} for {', '.join(s.entities)}")
        print("       whole frame moved, so this is framing, not continuity")

    for m in missing:
        print(f"  [{'missing':<16}] {m.entity}")
        print(f"       present in {', '.join(m.present_in)}")
        print(f"       absent from {', '.join(m.absent_from)}")

    for d in deltas:
        pairs = ", ".join(f"{take} has '{val}'" for take, val in sorted(d.outliers.items()))
        print(f"  [{d.severity:<16}] {d.entity}: {d.field}")
        print(f"       {len(d.agreeing_takes)} takes agree on '{d.expected}' ({', '.join(d.agreeing_takes)})")
        print(f"       {pairs}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
