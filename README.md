# Dailies

**A camera assistant wears smart glasses through a shoot and walks off set with the
paperwork already written.**

Live: **https://dailies-564641829203.us-east1.run.app**

Built for [Agentic Cinema](https://agentic-cinema.devpost.com/), ClickHouse track.

---

## The problem

Continuity is the job of making sure takes can be cut together: the mug on the same side
of the table, the jacket buttoned the same way, the phone in the same hand. It is tracked
by hand, on paper, take after take, and when it goes wrong nobody finds out until the edit,
when fixing it costs a day of crew.

Every other tool in this space starts from the screenplay PDF. This one starts from the set.

## What it does

The glasses capture the day. Gemini reads every take and extracts what is actually in
frame: props, wardrobe, positions, what moved. That becomes structured records in
ClickHouse. At wrap, instead of typing up a facing page, the crew asks:

> *which takes is the smartphone missing from?*

and the agent writes its own SQL, runs it through the ClickHouse MCP server, and answers.

The artifact is the crew member's paperwork. Catching a continuity break is a consequence
of having the records, not the identity of the product.

## Measured on real footage

Five real 3-minute takes of one desk, shot on Ray-Ban Meta glasses. Ground truth set by the
operator: **nothing was deliberately moved between takes.**

A naive extract-and-diff reported **8 differences, 7 of them fake**. Three passes, each
added because of a defect measured on this footage rather than anticipated, bring that to
**4 findings with no false alarms**:

| Stage | Findings | Fake |
|---|---|---|
| Naive extract + diff | 8 | 7 |
| + vocabulary reconciliation | 5 | 4 |
| + presence check | 6 | 3 |
| + camera-shift detection | **4** | **0** |

What survives is real, and nothing was told to look for any of it:

```
[camera move] take_005: 3 objects shifted depth together
              foreground -> midground for computer mouse, desk, mousepad
              the whole frame moved, so this is framing, not continuity
[gone]        computer mouse   absent from take_004
[gone]        smartphone       absent from take_005
[off mark]    smartphone       right in takes 1-2, center in takes 3-4
```

The mouse is genuinely gone from take 4 because both hands were holding a phone. The phone
is genuinely gone from take 5. And it genuinely moved from the desk to being held up
between takes 2 and 3.

## The pipeline

```
glasses ──► take.mp4 ──► ingest ──► frames + manifest
                                         │
                                         ▼
                                   extract  (Gemini, schema-constrained)
                                         │
                                         ▼
                                  reconcile  (one vocabulary across takes)
                                         │
                                         ▼
                                   presence  (re-examine what a pass missed)
                                         │
                                         ▼
                                   compare  ──► ClickHouse ──► ask (agent + MCP)
```

| Module | What it does, and why it exists |
|---|---|
| `pipeline/ingest.py` | Reduces a take to the frames worth reasoning about: scene changes merged with an even time sample, capped. A 3-hour shoot is millions of frames and almost all are redundant. |
| `pipeline/extract.py` | One Gemini call per take, all frames at once, under a response schema. Continuity state belongs to the take, and a controlled vocabulary is what makes takes comparable at all. |
| `pipeline/reconcile.py` | Resolves the same object appearing under different names. Gemini called one display `monitor` in two takes and `computer monitor` in three; exact-string grouping saw two objects and invented six continuity breaks out of synonyms. |
| `pipeline/presence.py` | Re-queries the frames for anything a take failed to mention, because an extraction pass is not exhaustive and silence is not absence. A recovered record must cite the timestamp it was seen at, so the claim is checkable. |
| `pipeline/compare.py` | Diffs takes on comparable fields, and separates a camera move from props actually moving. A prop moving is local; a camera moving is global. |
| `pipeline/load_clickhouse.py` | Schema and an idempotent loader. Replaces a scene's rows rather than appending: duplicates would corrupt the majority counts every finding rests on. |
| `pipeline/ask.py` | The agent. Hands the official `mcp-clickhouse` server to Gemini as a tool and lets it plan: inspect the schema, decide what to select, run it, read the rows, answer. |
| `web/` | The hosted surface, pre-loaded so a judge with no glasses can use it. |

### The agent is real, and it shows its work

No query for any demo question is hardcoded. Asked which takes the phone is missing from,
it ran four statements it chose itself. Asked to compare two takes it first wrote
`take_id IN (3, 4)`, got nothing back, and retried with `('take_003', 'take_004')`. That
self-correction only works because a failed query goes back to the model rather than
killing the request.

Every statement it runs is returned with the answer. A continuity note nobody can check is
not evidence.

## Run it

### Hosted

**https://dailies-564641829203.us-east1.run.app** — no setup, no glasses, no key.
Add `?theme=light` for the light palette.

### Locally

```bash
git clone https://github.com/JonathanSolvesProblems/dailies.git
cd dailies
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
DAILIES_OUT=samples .venv/bin/python -m uvicorn web.app:app --port 8077
```

Open http://127.0.0.1:8077. That works with **no credentials at all**, against the real
scene state in `samples/`. The question box appears only if a deployment can answer it.

To enable the agent, copy `.env.example` to `.env` and fill in a
[Gemini key](https://aistudio.google.com/apikey) and ClickHouse Cloud connection details,
then:

```bash
.venv/bin/python pipeline/load_clickhouse.py --out samples
.venv/bin/python pipeline/ask.py "which takes is the smartphone missing from?" --scene scene_a
```

### Processing your own footage

Needs `ffmpeg` and `ffprobe` on PATH.

```bash
python pipeline/ingest.py take_001.mp4 --out out/myscene --take-id take_001
python pipeline/extract.py out/myscene/take_001.json --scene-context "INT. KITCHEN - DAY"
python pipeline/reconcile.py out/myscene
python pipeline/presence.py out/myscene/reconciled
python pipeline/compare.py out/myscene/reconciled
```

## Stack

- **Gemini 3.6 Flash** via `google-genai`, falling back a generation when overloaded. Flash
  rather than Pro because this is a high-volume vision call on every take of a shoot day.
- **ClickHouse Cloud** via the official `mcp-clickhouse` MCP server. One denormalized
  `observations` table, `ORDER BY (scene_id, entity, take_id)`, because every question this
  asks is a filter and group-by over that.
- **FastAPI** on **Cloud Run**.
- **Ray-Ban Meta Gen 2** for capture. A DAT stream was measured holding 25 minutes and
  45,002 frames with no cap, so the 3-minute limit belongs to the stock camera app rather
  than the platform.

## Honest limitations

- **The demo scene is one desk, five takes.** The findings above are real and the ground
  truth is real, but this has not been run on a professional set.
- **`state` is descriptive, not comparable.** Across five takes of an identical scene the
  same mug was described "upright", "on napkin" and "placed on table". The diff runs only
  on normalized fields; a field that cannot be compared reliably is worse than no field,
  because a false continuity flag costs a crew real time chasing nothing.
- **Depth is a weak axis from POV capture.** It moves with the wearer's head, which is why
  camera-shift detection exists rather than reporting three breaks.
- **The hosted question box takes several seconds.** Cold start plus an MCP subprocess per
  request plus two round trips to Gemini.
- **Frames are not distributed.** `samples/` carries the scene state, not the footage, which
  is private video of a home.

## What is not here

No source footage, no extracted frames. The repo is public and the footage is not.
