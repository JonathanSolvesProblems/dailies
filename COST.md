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

Real extraction calls against real footage, `gemini-3.6-flash`, 2026-09-03:

| take | input tokens | output tokens | latency | cost |
|---|---|---|---|---|
| take_001 | 9,414 | 2,381 | 16.1s | $0.01599 |
| take_002 | 8,037 | 1,880 | 10.8s | $0.01308 |
| take_003 | 11,647 | 1,985 | 11.5s | $0.01618 |
| **mean** | **9,699** | **2,082** | **12.8s** | **$0.01508** |

n=3, not 5. Takes 004 and 005 returned `429 RESOURCE_EXHAUSTED` against a free-tier quota,
and two earlier attempts hit `503`. That is a quota limit, not a property of the workload,
but three samples is what was actually measured so three is what is reported.

At the current promotional rate of $0.75 / $3.75 per million tokens:

    9,699 / 1e6 x $0.75  +  2,082 / 1e6 x $3.75  =  $0.0151 per take

**About one and a half cents per take.**

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
  take** and $1.21 per 40-take day, still 0.08% of the cheapest shoot day.
- **The live check is a different model on a different budget.** `live.py` runs
  `gemini-3.5-flash`, which is $1.50 / $9.00, chosen for latency rather than price: 3.6
  Flash measured 11.3s against 3.5 Flash's 3.7s at identical recall, and a continuity check
  that lands after the take has ended is worthless. The rolling check trades money for
  seconds, deliberately.
- **Prices move.** Re-read the rate card before quoting anything here. A stale rate card
  turns an externally graded number back into an invented one.
