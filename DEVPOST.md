# Devpost submission

One field per block. Paste the block, nothing else. Notes are at the bottom, after every
final value, so a copy cannot pick them up by accident.

## Project name

```
Dailies
```

## Tagline

```
A camera assistant wears smart glasses through a shoot and walks off set with the continuity paperwork already written.
```

## Track

```
ClickHouse
```

## Hosted project URL

```
https://dailies-564641829203.us-east1.run.app
```

## Repository

```
https://github.com/JonathanSolvesProblems/dailies
```

## Description

```
A script supervisor stands just off set with a clipboard, and between every take writes down where the mug sat, whether the jacket was buttoned, which hand held the phone. It is the one job on a film shoot still done entirely on paper, and when a line gets missed nobody finds out until the edit. By then it is a pickup day: a published survey of US production markets prices a 6-person shoot day at $1,440 in the cheapest market and $3,020 in the most expensive.

Dailies does that job from a pair of Ray-Ban Meta glasses. The camera assistant wears them through the shoot. Gemini reads every take and extracts what is actually in frame: each prop, where it sits, what state it is in (a jacket buttoned or open, a glass full or empty, a phone in the left hand or the right). That becomes structured records in ClickHouse. At wrap the crew asks a question in plain English, "which takes is the smartphone missing from?", and a Gemini agent writes its own SQL, runs it through the official ClickHouse MCP server against a ClickHouse Cloud cluster, and answers, showing every statement it ran.

The rolling view is the same instrument in its operating mode. Pick a reference take, press Roll, and every few seconds the current camera frame is checked against what that reference recorded. When something contradicts it, the verdict is spoken into the wearer's ear through the glasses' own speakers, because a script supervisor is watching the scene, not a screen. Finding the mug on the wrong side at wrap documents a reshoot. Finding it twenty seconds into the take prevents one. Median verdict is 4.4 seconds on the deployed service, and every one of those checks is written back to ClickHouse with the take it was checking, so "which take did the rolling check flag most" is a query the agent can answer afterwards.

What it costs: 1.6 cents per take, measured on real footage through the same Vertex AI path the deployment runs, at Google's published rates. A 40-take day is about 63 cents, or 0.04% of the day it is guarding. Neither number is mine; Google meters the tokens and sets the price, the industry sets the day rate. What is deliberately not claimed is a count of reshoots prevented, because that would be my own code grading my own homework.

Technologies: Gemini 3.6 Flash and 3.5 Flash through Vertex AI (google-genai) for extraction, reconciliation, presence checking, the rolling check and the question agent; ClickHouse Cloud as the store behind every read path and as the target of the MCP-driven agent; the official mcp-clickhouse server; FastAPI on Cloud Run; a Kotlin app on the Meta Wearables Device Access Toolkit for the glasses; a browser rolling view that works from any webcam so a judge with no hardware can run all of it.

Data sources: the footage is my own, shot on Ray-Ban Meta Gen 2 glasses at a desk. The shoot-day cost is a published 2026 survey of US production markets. Model prices are Google's published rate card.

Findings and learnings, in the order they cost me something. A head-mounted camera breaks assumptions a tripod never would: every per-object depth change this project ever produced was the wearer leaning in, so depth now only counts as evidence when several objects move together, which is a camera move and is reported once. Gemini describes the same fact three different ways across takes, so free text is never compared, only a controlled vocabulary, and "unknown" is treated as no reading rather than a value, which is what stopped the mug from being flagged four times for being honestly unreadable. Flash-Lite was ten times faster and reported a fully mirrored room as correct, so speed bought by not looking is disqualified on recall, and I later repeated that exact mistake myself by turning thinking off on a prompt I had retyped instead of imported; a detector that never fires looks identical to one that works until something is wrong in front of it. The first version of the rolling view reported HOLDS six times in a row against a closed privacy shutter, because a black frame passes every size check and the model is honest about seeing nothing, so a frame is now measured for light before it is trusted. And the app ran for weeks with ClickHouse powering one endpoint out of eight while /api/health announced "json"; on a ClickHouse track that is the wrong way round, and it is fixed.
```

## Built with

```
gemini, vertex-ai, google-genai, clickhouse, mcp-clickhouse, mcp, fastapi, cloud-run, kotlin, jetpack-compose, meta-wearables-dat, python, javascript
```

## Video URL

```
(paste the public YouTube URL here once uploaded)
```

---

## Notes (not for pasting)

- Tagline is 126 characters. Devpost's limit is 200. Under.
- Description leads with a person (the script supervisor), per rules 12 and 22. No origin story is invented; the human is the real job this replaces.
- The 1.6 cents / 63 cents / 4.4s / $1,440 figures all match README.md and COST.md as of this commit. If any of those change, change them here in the same commit.
- The "findings and learnings" paragraph is the section the rules explicitly ask for. It is written as things that cost time, not as a feature list, because rule 28 says never lead with coverage.
- Video URL is the only empty field. Do not submit with it empty.
- Proofread every field in the RENDERED Devpost page, logged out, before the deadline (pre-submit gate check 3). The SWORN tagline shipped as drafting prose because a copy took the line above the block.
