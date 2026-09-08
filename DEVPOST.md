# Devpost submission

**Paste from `broll/submission.md`, not from this file.**

That file holds every field on the actual Devpost form as a final value, in the order the
form asks for them, including the seven Project Story headings the form pre-fills, the
partner-track answers, and the two "what did you use" boxes. This file used to hold a second
copy of the same text in a different shape, which is a drift hazard and a paste hazard at
once: two files with the same figures go out of sync, and the wrong block gets copied at
deadline. A previous entry of mine shipped its Devpost tagline as drafting prose for exactly
that reason.

`broll/submission.md` is gitignored, because it is working material rather than part of the
project. The story it contains is the same story as `README.md`, told for a different reader.

## Where the numbers live

Every figure quoted in the submission also appears in a tracked file, and the tracked file is
the authority. If one changes, change all of them in the same commit.

| figure | authority |
|---|---|
| 1.6 cents a take, 63 cents a 40-take day | `COST.md` |
| 4.4 s median rolling verdict | `README.md`, measured on the deployed service |
| 8 findings / 7 fake, down to 6 / 0 | `README.md`, "Measured on real footage" |
| $1,440 and $3,020 a shoot day | the Giggster 2026 survey, linked in `README.md` |
| ClickHouse code 497 on every write path | `README.md`, "The agent writes SQL, and cannot write data" |
| 29 tests | `tests/`, run `pytest` |

## The video

**https://www.youtube.com/watch?v=BOka9As4lVE**

2 min 31 s, under the 3 minute cap. Verified reachable without a login, with the title on
YouTube matching the one in `broll/submission.md`. That check cannot tell Public from
Unlisted, so confirm it is Public in YouTube Studio.

## Before submitting

- [x] Upload the video to YouTube and record the URL. Done, above and in
      `broll/submission.md`. Still paste it into the Devpost Video field.
- [ ] Confirm the video is **Public**, not Unlisted.
- [ ] Upload the eight images from `broll/preview/` in the order listed at the bottom of
      `broll/submission.md`.
- [ ] Confirm the ClickHouse "first time using" answer, which is the one field that could not
      be checked from the repository.
- [ ] Open the live project page in a private window, logged out, and read the name, the
      elevator pitch and the story top to bottom as a stranger.
- [ ] Search the hackathon's own gallery for "Dailies" and confirm it comes back. Being
      marked submitted is not the same as being findable.
