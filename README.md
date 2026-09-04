# Dailies

**A camera assistant wears smart glasses through a shoot and walks off set with the
paperwork already written.**

Live: **https://dailies-564641829203.us-east1.run.app**

Built for [Agentic Cinema](https://agentic-cinema.devpost.com/), ClickHouse track.

---

## The problem

Continuity is the job of making sure takes can be cut together: the mug on the same side
of the table, the jacket buttoned the same way, the phone in the same hand. It is tracked
by hand, on paper, take after take, and when it goes wrong nobody finds out until the edit.

By then it is not an edit problem. It is a pickup day. A published survey of US production
markets prices a 6-person, 10-hour shoot day at
[$1,440 in the cheapest market and $3,020 in the most expensive](https://giggster.com/guide/reports/film-shoot-costs-major-us-cities-2026/).

Dailies watches a take for **1.6 cents**, measured on real footage through the same Vertex AI
path the deployment runs, priced at [Google's published rates](https://ai.google.dev/gemini-api/docs/pricing).
A whole 40-take day costs about **63 cents**, or 0.04% of the day it is guarding.

Neither number is mine. Google meters the tokens and sets the price; the industry sets the
day rate. The workings are in [COST.md](COST.md), reproducible with
`python pipeline/measure.py`. What is deliberately *not* claimed anywhere here is a count of
reshoots prevented, because that would be my own code marking its own homework.

Every other tool in this space starts from the screenplay PDF. This one starts from the set.

## What it does

The glasses capture the day. Gemini reads every take and extracts what is actually in
frame: props, wardrobe, positions, what moved. That becomes structured records in
ClickHouse. At wrap, instead of typing up a facing page, the crew asks:

> *which takes is the smartphone missing from?*

and the agent writes its own SQL, runs it through the ClickHouse MCP server, and answers.

The artifact is the crew member's paperwork. Catching a continuity break is a consequence
of having the records, not the identity of the product.

## Rolling: catching it before the take is spoiled

`/live` is the same instrument in its operating mode. Pick a reference take, press **Roll**,
and every few seconds it checks what the camera sees against what that reference recorded.
The verdict is one word, large enough to read from across the room, because the operator is
watching the scene and not this screen.

The report view answers *what went wrong today*. This answers *what is wrong right now*,
which is the question the job actually asks. Finding the mug on the wrong side at wrap has
documented a reshoot. Finding it twenty seconds into the take has prevented one.

A judge with no hardware can run all of this from a laptop webcam, and should. But the
glasses are the instrument this is built for, not an interchangeable frame source, and the
difference is the whole reason the rolling check works.

### Why head-mounted, and not a tripod

Continuity is an attention problem before it is a vision problem. A script supervisor does
not scan the room uniformly; they look at the things that can betray a cut, and they look at
them **between** takes, while walking, with a clipboard already in both hands. A tripod
camera sees the set. Glasses see what the person responsible for continuity actually checked,
which is a different and much better-aimed signal.

Two consequences the code had to be built around, both learned from real footage rather than
assumed:

- **Depth stops being a property of the object.** From a head-mounted camera, a mug moving
  from midground to foreground usually means the wearer leaned in. Every per-object depth
  delta this project ever produced was a false positive, so depth now only survives as
  evidence that *several* objects moved together, which is a camera move and is reported once
  instead of as N continuity breaks. A tripod would never have forced that distinction.
- **Framing is never stable, so position must be relative.** The frame edges move constantly
  with the wearer's head, which is why position is judged against the other objects in shot
  rather than against the crop.

And the output has to be audio. The wearer is watching the scene, not a screen, with their
hands full. These glasses have no display, so a verdict spoken into the ear is not a
consolation prize for missing hardware; it is the only delivery that works on a set. That is
also why the rolling check has to answer inside a take rather than at wrap.

**Latency: 2.2s median, 2.8s p90**, measured over eight consecutive checks against the
deployed service, with the negative control quiet on all eight. A take runs well over thirty
seconds, so a dozen or more checks land while the camera is still rolling.

That number was 16.4s two hours before this was written, and the way down is worth recording
because neither cause was where it looked:

| | median |
|---|---|
| after the move to Vertex AI | 16.4s |
| building the Gemini client once instead of per frame | 9.3s |
| turning off thinking, recall re-tested | **2.2s** |

The first was invisible for a while because the service's own timing said the delay was
inside the model call, which pointed at Vertex having got slower. It had not: the identical
request issued directly still answered in four seconds. `make_client()` ran per request, and
on Vertex that performs credential discovery, which on Cloud Run is a metadata-server round
trip for a service-account token. The API-key path has no such step, so the cost only
appeared once the credential changed. Nothing had got slower except how often the service was
asking who it was.

The second is in `pipeline/live.py` with its measurements attached. Thinking was disabled only
after the control-and-mirror test showed recall unchanged, which is the check Flash-Lite
failed.

One caveat that matters for recording a demo: these are warm-instance figures. The first call
after a cold start once took 182 seconds while the container built its first Vertex
credential. Send one throwaway request before rolling.

Checks run sequentially rather than on a fixed timer; polling faster than the answer arrives
just stacks requests until the queue collapses.

### Choosing the model by measuring recall and latency together

Two findings, both from measurement rather than reasoning, and the second only exists
because the first was so expensive.

**Flash-Lite is disqualified on recall, not speed.** It benchmarked about ten times faster,
979ms against 5-15s. Then it was handed a horizontally mirrored frame, every object on the
desk on the wrong side, and reported:

> *"Laptop, monitor, coffee mug, and microphone are in their expected positions."*

The speed was not a tradeoff, it was an artefact of not looking. A detector with no recall
is a green light wired to nothing, and on a set that is worse than no tool because someone
will trust it.

**Gemini 3.5 Flash beats 3.6 Flash here**, three times faster at identical recall. Control
frame (must stay silent) and mirrored frame (must flag), three runs each side:

| model | frame | median | false alarms | caught |
|---|---|---|---|---|
| 3.6-flash | 512px | 11263ms | 0/3 | 3/3 |
| 3.6-flash | 384px | 24268ms | 0/3 | 3/3 |
| **3.5-flash** | **512px** | **3678ms** | **0/3** | **3/3** |
| 3.5-flash | 384px | 3902ms | 0/3 | 3/3 |

The newer model is slower without being better at this task, and frame size barely moves
the number, so the lever is the model rather than the pixels. Note 3.6 at 384px being
*slower* than at 512px: that is server-side variance, and a good argument for measuring a
configuration rather than reasoning about it.

Confirmed at twelve runs after the switch: 0 false alarms, 6 of 6 caught, 4.2s median. That
4.2s was the figure with thinking still on and a client built per request; both were fixed
later and the check now runs at 2.2s median. The model comparison above is unaffected, since
both models were measured under the same conditions as each other.

Flash-Lite benchmarked about ten times faster on this exact call, 979ms against 5-15s, and
was chosen on that basis. Then it was handed a horizontally mirrored frame, every object on
the desk on the wrong side, and reported:

> *"Laptop, monitor, coffee mug, and microphone are in their expected positions."*

Flash, on the same frame, flagged the mug, microphone, mouse and mousepad at 0.95 each and
named the swap. The speed was not a tradeoff, it was an artefact of not looking. A detector
with no recall is not a fast detector, it is a green light wired to nothing, and on a set
that is worse than no tool because someone will trust it.

Verified after the change: control frame silent on 4 of 4 runs, mirrored frame flagged on
4 of 4 with the correct objects.

## Measured on real footage

Five real 3-minute takes of one desk, shot on Ray-Ban Meta glasses. Ground truth set by the
operator: **nothing was deliberately moved between takes.**

A naive extract-and-diff reported **8 differences, 7 of them fake**. Four passes, each added
because of a defect measured on this footage rather than anticipated, bring that to
**6 findings with no false alarms**:

| Stage | Findings | Fake |
|---|---|---|
| Naive extract + diff | 8 | 7 |
| + vocabulary reconciliation | 5 | 4 |
| + presence check | 6 | 3 |
| + camera-shift detection | 4 | 0 |
| + state vocabulary | **6** | **0** |

The last row adds findings rather than removing them, which is the point of it. Reconciling,
presence and camera-shift all exist to delete false alarms; giving the model a controlled
state vocabulary instead of free text let it report two things it had been seeing all along
and had no way to say. Both are real: the mouse is held in take_001 and resting in the other
three, and the phone is in both hands in take_004 and untouched in take_003.

What survives is real, and nothing was told to look for any of it:

```
[camera move] take_005: 3 objects shifted depth together
              foreground -> midground for computer mouse, desk, mousepad
              the whole frame moved, so this is framing, not continuity
[gone]        computer mouse   absent from take_004
[gone]        smartphone       absent from take_005
[off mark]    smartphone       right in takes 1-2, center in takes 3-4
[state]       computer mouse   held=right_hand in take_001, resting in 002/003/005
[state]       smartphone       held=both_hands in take_004, not held in take_003
```

The mouse is genuinely gone from take 4 because both hands were holding a phone. The phone
is genuinely gone from take 5. And it genuinely moved from the desk to being held up
between takes 2 and 3.

The two `[state]` lines are the same story told from the other side: the reason the mouse
vanishes in take_004 is the reason the phone is in both hands in take_004. A facing page that
records only presence and position cannot say that, which is exactly why the state vocabulary
was added.

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
| `pipeline/live.py` | The rolling check. One frame against a reference take's state, asked as a closed question rather than an open description, because that is both faster and far more accurate. Stateless, because a check that needs the previous frame cannot survive a dropped connection and sets drop connections. |
| `web/` | The hosted surface, pre-loaded so a judge with no glasses can use it. `/` is the report, `/live` is the rolling check. |

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
  Reached through **Vertex AI** (`location="global"`), so the deployed service authenticates
  as its own Cloud Run service account and no API key exists in the deployment at all. The
  region is measured, not chosen: us-east1 does not serve 3.6 Flash and us-central1 404s on
  both 3.5 and 3.6, so only `global` serves every model this uses.
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
- **The glasses can speak but cannot listen.** The obvious next feature is the operator
  saying "mark take 3" instead of touching a phone, and it is not buildable on DAT 0.9.0.
  The public `Stream` interface exposes `videoStream`, `capturePhoto`, `state`, `errorStream`
  and `start`/`stop`, and no audio input. `AudioFrame`, `AudioDecoder` and
  `MetaWearablesDATAudioEventListener` do ship inside `mwdat-camera`, so the capability
  plainly exists, but reaching it would mean binding to internal classes that any SDK release
  can move. Output is unaffected: the glasses register as an ordinary Bluetooth audio device,
  so the spoken verdict needs no SDK support at all.
- **Frames come from `capturePhoto`, not the video stream.** `getVideoStream()` is available
  and would be the deeper use of the hardware, but it delivers encoded frames needing a
  MediaCodec pipeline, and a check every few seconds does not need 24fps. The photo path
  returns a Bitmap and is the honest fit for the duty cycle.
- **The hosted question box takes several seconds.** Cold start plus an MCP subprocess per
  request plus two round trips to Gemini.
- **Frames are not distributed.** `samples/` carries the scene state, not the footage, which
  is private video of a home.

## What is not here

No source footage, no extracted frames. The repo is public and the footage is not.
