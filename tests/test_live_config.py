"""Guards on the live check's configuration.

These are cheap, offline assertions standing in for an expensive one. The real guarantee is
the control-and-mirror recall test, which needs live model calls and cannot run in CI, so
what is checked here is the set of conditions under which that recall was last verified. If
one of these changes, the recall test has to be re-run before the change ships.

The specific incident: thinking was disabled on the strength of a benchmark that rebuilt the
prompt by hand instead of importing it. The two texts differed by one closing sentence, and
against the real one the setting was blind to a fully mirrored room, 0 of 3 where the harness
had measured 3 of 3. The negative control still passed, so nothing looked wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import live  # noqa: E402


class TestPromptIsImportedNotRetyped:
    """The prompt has one definition, and it is reachable."""

    def test_build_prompt_is_public(self):
        assert callable(live.build_prompt)

    def test_prompt_contains_the_sentence_recall_depends_on(self):
        """The closing question is load-bearing, not decoration.

        Dropping it is what made thinking-off appear safe. Any edit to this sentence
        invalidates the last recall measurement.
        """
        p = live.build_prompt([{"entity": "mug", "position_h": "left"}])
        assert "What contradicts the reference?" in p
        assert p.startswith("REFERENCE TAKE recorded this:")

    def test_scene_context_is_optional_and_appended(self):
        base = live.build_prompt([{"entity": "mug", "position_h": "left"}])
        with_ctx = live.build_prompt([{"entity": "mug", "position_h": "left"}], "a kitchen")
        assert "a kitchen" in with_ctx and "a kitchen" not in base


class TestThinkingStaysOn:
    """Disabling thinking cost all recall on the mirrored frame. It must not come back silently."""

    def test_source_does_not_disable_thinking(self):
        src = (ROOT / "pipeline" / "live.py").read_text(encoding="utf-8")
        offending = [
            ln.strip()
            for ln in src.splitlines()
            if "thinking_budget=0" in ln.replace(" ", "") and not ln.strip().startswith("#")
        ]
        assert not offending, (
            "thinking_budget=0 is uncommented in live.py. With the production prompt this "
            "caught 0/3 mirrored frames. Re-run the control-and-mirror recall test against "
            "build_prompt() before shipping this."
        )


class TestRecallCriticalConstants:
    """Values the last recall run was measured at."""

    def test_model_is_the_measured_one(self):
        assert live.DEFAULT_MODEL == "gemini-3.5-flash"

    def test_confidence_floor_unchanged(self):
        # The mirrored frame is reported at 0.90 to 0.95, so this floor has headroom.
        # Raising it past 0.90 would start dropping true positives.
        assert live.MIN_CONFIDENCE == 0.65
        assert live.MIN_CONFIDENCE < 0.90
