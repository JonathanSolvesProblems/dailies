"""Regression tests for the diff logic.

Every test here encodes a false positive this project actually shipped and then had to fix.
That is deliberate: the value of Dailies is not that it finds differences, which is easy, but
that it does not cry wolf. A naive diff over the same five takes produced eight findings of
which seven were fake. The reconciliation rules below are what took it to six real ones, and
each rule is one measured mistake.

Tests that only proved the happy path would not have caught any of these, because in every
case the code was working exactly as written and the output was still wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.compare import CAMERA_SHIFT_THRESHOLD, compare  # noqa: E402


def observation(entity: str, **fields) -> dict:
    base = {
        "entity": entity,
        "position_h": "center",
        "depth": "midground",
        "state_value": "na",
        "state_class": "none",
        "confidence": 0.9,
    }
    base.update(fields)
    return base


def scene(**takes: list[dict]) -> dict[str, dict]:
    return {
        take_id: {"take_id": take_id, "observations": obs}
        for take_id, obs in takes.items()
    }


class TestUnknownIsNotAValue:
    """The mug bug.

    Gemini read the mug's fill level as `unknown` in four takes, because it could not see
    into the cup, and `half` in the one take where the angle allowed it. The diff compared
    those five strings, found four-against-one, and reported a likely continuity break.

    Nothing had changed. The model had been honest about not knowing, four times, and the
    diff turned that honesty into evidence of change. This is the same error as treating an
    entity's absence from a frame as proof it left the room.
    """

    def test_unknown_against_a_real_value_is_not_a_break(self):
        states = scene(
            take_001=[observation("coffee mug", state_value="unknown")],
            take_002=[observation("coffee mug", state_value="unknown")],
            take_003=[observation("coffee mug", state_value="unknown")],
            take_004=[observation("coffee mug", state_value="unknown")],
            take_005=[observation("coffee mug", state_value="half")],
        )
        deltas, _, _ = compare(states)
        assert [d for d in deltas if d.field == "state_value"] == []

    def test_na_is_also_not_a_value(self):
        states = scene(
            take_001=[observation("lamp", state_value="na")],
            take_002=[observation("lamp", state_value="on")],
        )
        deltas, _, _ = compare(states)
        assert [d for d in deltas if d.field == "state_value"] == []

    def test_but_two_real_values_disagreeing_IS_a_break(self):
        """The rule must not be so eager that it suppresses the thing we exist to find."""
        states = scene(
            take_001=[observation("wine glass", state_value="full")],
            take_002=[observation("wine glass", state_value="full")],
            take_003=[observation("wine glass", state_value="empty")],
        )
        deltas, _, _ = compare(states)
        breaks = [d for d in deltas if d.field == "state_value"]
        assert len(breaks) == 1
        assert breaks[0].expected == "full"
        assert breaks[0].outliers == {"take_003": "empty"}
        assert breaks[0].severity == "likely break"


class TestDepthIsNeverAPerObjectClaim:
    """The head-mounted-camera bug.

    Across every run of this project, a per-object depth delta was a false positive. The
    camera is on someone's head. When they lean in, the mug "moves" from midground to
    foreground while sitting perfectly still on the table.

    Depth is a fact about where the operator's head is, not about where the object is, so a
    single object changing depth carries no information about that object at all.
    """

    def test_one_object_changing_depth_is_dropped(self):
        states = scene(
            take_001=[observation("coffee mug", depth="midground")],
            take_002=[observation("coffee mug", depth="midground")],
            take_003=[observation("coffee mug", depth="foreground")],
        )
        deltas, _, _ = compare(states)
        assert [d for d in deltas if d.field == "depth"] == []

    def test_depth_change_does_not_mask_a_real_state_change(self):
        """Dropping depth must not drop the object's other, real findings with it."""
        states = scene(
            take_001=[observation("jacket", depth="midground", state_value="buttoned")],
            take_002=[observation("jacket", depth="midground", state_value="buttoned")],
            take_003=[observation("jacket", depth="foreground", state_value="unbuttoned")],
        )
        deltas, _, _ = compare(states)
        fields = {d.field for d in deltas}
        assert "depth" not in fields
        assert "state_value" in fields


class TestCameraMoveIsOneFactNotN:
    """The desk bug.

    In take_005, `desk`, `computer mouse` and `mousepad` all went foreground -> midground
    together. Nothing on that desk moved; the wearer sat back, and from head-mounted POV the
    whole frame reparallaxes at once.

    Reported as three continuity breaks, that buries the two real findings in the same list.
    A prop moving is a local event; a camera moving is a global one, and counting how many
    entities moved together is what separates them.
    """

    def test_three_objects_moving_together_is_one_camera_shift(self):
        moved = {"position_h": "left"}
        states = scene(
            take_001=[observation(e) for e in ("desk", "mouse", "mousepad")],
            take_002=[observation(e) for e in ("desk", "mouse", "mousepad")],
            take_003=[observation(e, **moved) for e in ("desk", "mouse", "mousepad")],
        )
        deltas, _, shifts = compare(states)
        assert len(shifts) == 1
        assert shifts[0].take_id == "take_003"
        assert len(shifts[0].entities) == 3
        # and they are NOT also reported individually
        assert [d for d in deltas if d.field == "position_h"] == []

    def test_below_the_threshold_stays_a_normal_finding(self):
        """Two objects moving is still two props moving, not a camera move."""
        assert CAMERA_SHIFT_THRESHOLD == 3
        moved = {"position_h": "left"}
        states = scene(
            take_001=[observation(e) for e in ("mug", "phone")],
            take_002=[observation(e) for e in ("mug", "phone")],
            take_003=[observation(e, **moved) for e in ("mug", "phone")],
        )
        deltas, _, shifts = compare(states)
        assert shifts == []
        assert len([d for d in deltas if d.field == "position_h"]) == 2

    def test_scattered_values_are_not_a_camera_move(self):
        """Three objects moving to three DIFFERENT places is chaos, not a pan."""
        states = scene(
            take_001=[observation(e) for e in ("a", "b", "c")],
            take_002=[observation(e) for e in ("a", "b", "c")],
            take_003=[
                observation("a", position_h="left"),
                observation("b", position_h="right"),
                observation("c", position_h="left"),
            ],
        )
        _, _, shifts = compare(states)
        assert shifts == []


class TestPresence:
    def test_entity_absent_from_one_take_is_reported_as_missing(self):
        states = scene(
            take_001=[observation("smartphone")],
            take_002=[observation("smartphone")],
            take_003=[],
        )
        _, missing, _ = compare(states)
        assert len(missing) == 1
        assert missing[0].entity == "smartphone"
        assert missing[0].absent_from == ["take_003"]

    def test_low_confidence_readings_are_ignored(self):
        """A shaky read is not a continuity break."""
        states = scene(
            take_001=[observation("mug", state_value="full")],
            take_002=[observation("mug", state_value="full")],
            take_003=[observation("mug", state_value="empty", confidence=0.2)],
        )
        deltas, _, _ = compare(states)
        assert [d for d in deltas if d.field == "state_value"] == []


class TestQuietWhenNothingChanged:
    """The negative control.

    A continuity tool that finds something in every scene is worthless, because the crew
    stops reading it. Identical takes must produce silence.
    """

    def test_identical_takes_produce_no_findings(self):
        obs = [
            observation("mug", state_value="half", position_h="left"),
            observation("laptop", state_value="on", position_h="center"),
            observation("jacket", state_value="buttoned", position_h="right"),
        ]
        states = scene(take_001=list(obs), take_002=list(obs), take_003=list(obs))
        deltas, missing, shifts = compare(states)
        assert (deltas, missing, shifts) == ([], [], [])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
