# What it costs to run

Every accuracy number this project could produce would be my code scored against ground
truth I set, on footage I shot. That is a demo, not a result, so this file does not report
one. It reports the one figure neither half of which is mine to influence:

- **token counts** come from the Gemini API's own `usage_metadata` on the response
- **prices** come from [Google's published rate card](https://ai.google.dev/gemini-api/docs/pricing)
- **the shoot-day comparison** comes from a
  [published industry survey](https://giggster.com/guide/reports/film-shoot-costs-major-us-cities-2026/),
  not from a figure chosen here

Reproduce with `python pipeline/measure.py --scene out/scene_a`.

## Measured

Every successful extraction call against real footage, `gemini-3.6-flash`, 2026-09-03.
Seven calls over four distinct takes, run twice where the quota allowed:

| take | input | output | latency | cost |
|---|---|---|---|---|
| take_001 | 9,414 | 2,381 | 16.1s | $0.01599 |
| take_001 | 9,414 | 2,466 | 105s † | $0.01631 |
| take_002 | 8,037 | 1,880 | 10.8s | $0.01308 |
| take_002 | 8,037 | 1,328 | 18.4s | $0.01101 |
| take_003 | 11,647 | 1,985 | 11.5s | $0.01618 |
| take_003 | 11,647 | 1,432 | 10.1s | $0.01411 |
| take_004 | 11,645 | 2,400 | 987s † | $0.01773 |
| **mean** | **9,977** | **1,982** | **11.5s** ‡ | **$0.01491** |

At the current promotional rate of $0.75 / $3.75 per million tokens:

    9,977 / 1e6 x $0.75  +  1,982 / 1e6 x $3.75  =  $0.0149 per take

**About one and a half cents per take**, ranging $0.011 to $0.018.

† **Contaminated, by my own mistake.** I left one measurement process running and started a
second against the same free-tier quota. Those two calls were queued behind the other
process, not doing 105 and 987 seconds of work. They are listed rather than deleted because
removing an inconvenient sample silently is how a measurement becomes a claim, but they say
nothing about the workload.

‡ Median of the five uncontended calls (10.1, 10.8, 11.5, 16.1, 18.4s). The token counts
from the contaminated calls are unaffected, since queueing does not change what was sent, so
all seven count toward cost.

Take 005 never completed: `429 RESOURCE_EXHAUSTED` on every attempt.

**Output length is not stable at temperature 0.** The same take produced 1,880 and 1,328
output tokens on two runs, a 42% spread, and take_003 varied 39%. Input is byte-identical
every time; the model's answer length simply varies. So per-take cost is a distribution, not
a constant, and a single sample would have been an anecdote. This is the main reason the
table above lists every call instead of one run.

## Re-measured after the move to Vertex AI

The deployment now reaches Gemini through Vertex AI rather than an AI Studio API key, so the
figures above were re-measured on the new path. Same three takes, same model, same day:

| take | input | output | cost |
|---|---|---|---|
| take_001 | 10,017 | 2,062 | $0.01525 |
| take_002 | 8,640 | 2,207 | $0.01476 |
| take_003 | 12,250 | 2,178 | $0.01736 |
| **mean** | **10,302** | **2,149** | **$0.01579** |

**$0.0158 per take, 4.7% above the $0.0149 measured through the API key.** The headline is
unchanged and a 40-take day is $0.63 rather than $0.60.

The difference is real but is in the token counts, not the rates: Vertex reports about 600
more input tokens per call for byte-identical input, consistently across all three takes,
which is presumably how the system instruction is counted. Worth recording because it is the
kind of gap that looks like a pricing change and is not.

**What I could not verify.** Both of Google's pricing pages render their tables in a way this
project's tooling could not read, so the $0.75 / $3.75 rate above is taken from the
Gemini API rate card and applied to the Vertex figures on the assumption the promotional rate
is common to both. One secondary source claims the Developer API is $1.50 / $7.50 while
Vertex is $0.75 / $3.75, which contradicts Google's own page as read on 2026-09-03. If the
higher rate is the correct one for either path, every figure here doubles: $0.032 per take
and $1.26 per 40-take day, which is still 0.09% of the cheapest shoot day and does not change
the argument. Check the rate card before quoting a precise figure anywhere it matters.

## The comparison

The published survey prices a fixed 6-person, 10-hour shoot day, weighting 60% crew, 30%
location, 10% permits. It puts the cheapest US market, Santa Fe NM, at **$1.44K per day**
and the most expensive, Boston MA, at **$3.02K per day**.

A continuity error that reaches the edit is not fixed in the edit. It is fixed with a pickup
day, which is one of those days.

So the asymmetry, stated plainly:

| | cost |
|---|---|
| one pickup day, cheapest US market | **$1,440** |
| running a whole shoot day through Dailies | **$0.60** |

The $0.60 assumes **40 takes in a day**, which is my assumption and not a sourced figure.
The per-take number is the measured one; scale it to whatever a given production actually
shoots. At 40 takes, the tool costs **0.04%** of the day it is guarding.

## What this number is not

It is not a claim that Dailies has prevented a reshoot. It has not been used on a real
production, and counting reshoots I claim to have prevented would be exactly the self-graded
figure this file exists to avoid.

It is the ratio between two prices set by other people: what Google charges to run it, and
what the industry pays for the day it protects. Both are checkable without trusting me.

## Caveats

- **The promotional rate expires.** Gemini 3.6 Flash is $0.75 / $3.75 through 2026-12-31 and
  rises to $1.50 / $7.50 on 2027-01-01. After that the same measurement is **$0.030 per
  take** and $1.19 per 40-take day, still 0.08% of the cheapest shoot day.
- **The live check is a different model on a different budget.** `live.py` runs
  `gemini-3.5-flash`, which is $1.50 / $9.00, chosen for latency rather than price: 3.6
  Flash measured 11.3s against 3.5 Flash's 3.7s at identical recall, and a continuity check
  that lands after the take has ended is worthless. The rolling check trades money for
  seconds, deliberately.
- **Prices move.** Re-read the rate card before quoting anything here. A stale rate card
  turns an externally graded number back into an invented one.
