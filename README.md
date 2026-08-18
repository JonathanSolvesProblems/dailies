# Dailies

A camera assistant wears smart glasses through a shoot and walks off set with the
paperwork already written.

Built for [Agentic Cinema](https://agentic-cinema.devpost.com/), ClickHouse track.

| | |
|---|---|
| Submissions close | **Sept 9 2026, 2:00 PM PDT** |
| Judging | **Sept 24 to Oct 8 2026** |
| Winners | Oct 13 2026 |

The judging window is the number that actually constrains infrastructure. The hosted URL
and the ClickHouse cluster must both be alive and responsive from Sept 24 to Oct 8, which
is **two weeks after** the last commit. Anything on a trial clock has to be checked against
Oct 8, not against the submission date.

## What it is

Every other script tool starts from the screenplay PDF. This one starts from the set.

The glasses capture the day. Gemini watches every take and extracts what is actually in
frame: props, wardrobe, positions, who is where. That becomes structured records in
ClickHouse. At wrap, instead of typing up a facing page by hand, the crew queries it:

> "which takes have the coffee mug on the left side of the table?"

Takes 1, 2 and 4. Take 3 does not. That is a continuity break, found in seconds, on real
footage, against a database that did not exist that morning.

The artifact is the crew member's paperwork. The continuity catch is a consequence of
having the records, not the identity of the product.

## Architecture

```
glasses ──► take (mp4) ──► ingest.py ──► frames + manifest
                                              │
                                              ▼
                                    Gemini (scene state)
                                              │
                                              ▼
                                        ClickHouse
                                              │
                                              ▼
                              hosted web app  ◄── judges test here
```

**Two capture paths, on purpose.** Judges have no glasses, and the rules require a hosted
URL they can test. So the hosted app is fully explorable against a pre-loaded shoot, while
the demo video shows real capture. Same data, two ways in.

## Status

| Piece | State |
|---|---|
| `pipeline/ingest.py` | **Working.** Scene detection + even sampling, frame extraction, manifest |
| `pipeline/extract.py` | **Working.** Gemini 3.6 Flash, schema-constrained scene state |
| `pipeline/compare.py` | **Working.** Cross-take continuity diff |
| ClickHouse schema + writes | Not started, blocked on account |
| Hosted web app | Not started |
| Live capture app (glasses) | Not started, deliberately second. See below |

### Measured on real footage: five 3-minute takes of one desk

Ground truth set by the operator: nothing on the desk was deliberately moved between takes.

A naive extract-and-diff reported **8 problems, 7 of them fake**. Three passes, each added
because of a defect measured on this footage rather than anticipated, brought that to **4
findings with no false alarms**:

| Stage | Findings | Fake |
|---|---|---|
| Naive extract + diff | 8 | 7 |
| + vocabulary reconciliation | 5 | 4 |
| + presence check | 6 | 3 |
| + camera-shift detection | **4** | **0** |

What the three passes fix, in order:

1. **Reconciliation.** Gemini called the same display `monitor` in two takes and
   `computer monitor` in three. Exact-string grouping saw two objects, each "missing" from
   wherever the other name was used. Six phantom breaks from synonyms.
2. **Presence check.** An extraction pass is not exhaustive, so an unmentioned desk was read
   as a vanished desk. The system now re-queries the frames for anything a take failed to
   mention, and a recovered observation must cite the timestamp it was seen at, so the claim
   is checkable rather than an impression.
3. **Camera-shift detection.** In take 5, `desk`, `computer mouse` and `mousepad` all moved
   foreground to midground together. Nothing moved; the wearer sat further back. A prop
   moving is a local event, a camera moving is a global one, and counting how many objects
   moved together separates them.

What survives is real:

```
[camera move     ] take_005: 3 objects shifted depth together
     foreground -> midground for computer mouse, desk, mousepad
     whole frame moved, so this is framing, not continuity
[missing         ] computer mouse    absent from take_004
[missing         ] smartphone        absent from take_005
[possible break  ] smartphone: position_h
     2 takes agree on 'right' (take_001, take_002)
     take_003 has 'center', take_004 has 'center'
```

The mouse is genuinely gone from take 4 because both hands were holding a phone. The phone
is genuinely gone from take 5. And it genuinely moved from the desk on the right to held up
in the centre between takes 2 and 3. Nobody told the system to look for any of that.

### The core claim is proven end to end

Four synthetic takes of the same scene, identical except that the mug sits on the left in
takes 1, 2 and 4 and on the right in take 3. Nothing was told about the planted break.

```
Scene: 4 takes (take_001, take_002, take_003, take_004)

  [likely break    ] coffee mug: position_h
       3 takes agree on 'left' (take_001, take_002, take_004)
       take_003 has 'right'
```

Two things matter more than the catch itself. Gemini named the entity `coffee mug` in all
four takes, which is what makes any comparison possible. And the laptop and table, which
did not change, produced **no** flags. A continuity tool that cries wolf costs a crew more
time than it saves.

### Why the live capture app is second, not cut

It is the moat: almost nobody entering will have working POV camera access, and we have
already proven a DAT stream holds 25 minutes and 45,002 frames with no cap. It gets built.

But v1 does not depend on it. The glasses record natively to the Meta AI app, which yields
MP4 files the pipeline can consume today. That means the expensive, uncertain half (does
Gemini actually extract usable scene state?) gets answered against real footage before any
Android work starts. If that answer is no, the app would have been wasted effort.

## Verified so far

`ingest.py` reduces a take to the frames worth reasoning about. A three hour shoot is
millions of frames and almost all are redundant, so it merges two strategies: scene
changes (where state actually moves) and an even time sample (so a static take is still
covered), capped at `--max-frames`.

Tested on a 12 second clip with three hard cuts at t=3, 6, 9:

```
take cuts: 12.0s 640x360
  7 frames kept (3 scene changes, 4 sampled)

index timestamp_s source
    0       1.714 sample
    1         3.0 scene
    2       5.143 sample
    3         6.0 scene
    4       6.857 sample
    5         9.0 scene
    6      10.286 sample
```

All three cuts found, and the extracted frame at t=6.0 is visually confirmed to be the
segment that starts at t=6. Timestamps and pixels agree, which matters because the
manifest is what Gemini is told.

### A tuning bug worth recording

The first version used a scene threshold of 0.25 and found **one** of the three cuts. At
0.05 it found all three. The default is now 0.10.

The asymmetry is what decides it: a missed scene change is a missed piece of scene state,
which is the entire product, while an extra frame costs a fraction of a cent and gets
capped anyway. Recall beats precision here.

Within a single take there are usually no cuts at all, so in practice this detects "the
picture changed materially": an actor moved, a prop was set down, the light shifted. Those
are exactly the moments worth a frame.

## Running it

```powershell
python pipeline\ingest.py take_003.mp4 --out out --take-id take_003
python pipeline\ingest.py take_003.mp4 --out out --scene-threshold 0.05 --max-frames 20
```

Needs `ffmpeg` and `ffprobe` on PATH. No Python dependencies yet.
