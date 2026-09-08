# B-roll

Footage captured for the demo video. Not distributed: this is video of a real home and the
people in it, and the repo is public.

The finished cut is public, though, and is what these clips were gathered for:
**https://www.youtube.com/watch?v=BOka9As4lVE** (2 min 31 s). If it is ever re-cut, the
render command and the two traps are further down this file.

Suggested layout:

    broll/glasses/     unboxing, putting them on, the capture LED
    broll/setup/       laying the table, the reference take
    broll/catch/       the moment the app calls it, phone screen visible
    broll/screen/      screen recordings of the report and the rolling view

## What has already been captured (2026-09-06)

Everything below was produced by driving the real deployed service, not a mock.

    web-live/rolling-desktop.mp4     63s, 1440x900. The actual /live page in Chrome, fed real
                                     take_001 frames as a fake webcam for 12s then the same
                                     frames mirrored for 12s, looping. Verdict goes HOLDS at
                                     5.6s, OFF THE MARK (coffee mug, right -> left) at 20.1s,
                                     HOLDS at 35.7s, OFF THE MARK at 43.8s. Recall and the
                                     negative control, on one clip, following the feed.
    web-live/stills/                 frames at 8, 20, 24, 30, 35, 47, 50, 52s

    mobile-live/rolling-phone.mp4    70s, 1080x2340. A Galaxy S24 running /live in Chrome
                                     on its real rear camera, pointed at a hallway. The
                                     camera picker appears on its own ("camera 2, facing
                                     back"). Reads HOLDS throughout, which is correct: the
                                     desk objects are not visible, and not visible is not
                                     moved. The first second shows the site camera prompt.
    mobile-live/15..18-*.png         stills: the prompt, then +16s, +40s, +52s

    apk-test/                        the Android app on the same phone: launch, permission,
                                     standby, insets bug before and after, Roll with no
                                     glasses timing out to the "pair them" message

    2026-09-04 17-01-47.mp4          your own screen recording of the report view

What is NOT here, and cannot be made without the glasses on your face: the verdict arriving
in your ear, the phone changing hands in a real take, and the quiet take that stays quiet.
Those are the demo.

The one shot the whole video needs: press Roll, move the thing, and let the pause be real
before the verdict lands. It answers in about four seconds. Do not cut that beat out: the pause between the
change and the verdict is the shot.

## What is in here now (2026-09-08)

    narrative.mp4            the raw narration recording, audio only matters
    narration.mp3            extracted from it, 44.1 kHz mono
    narration.trim.mp3       the same with 1.35s of head silence removed. This is the file
                             vidkit is given, so the first word lands at 1.87s instead of 3.62s.
    headshot.mp4             18.1s of me wearing the glasses. The audio track is digital
                             silence at -91 dB and the mouth moves throughout, so it is used
                             muted, 2.8s, from t=13.0 which is the stillest window in it.
    demo.edit-plan.json      the hand-written 19-segment plan. Re-render with:
                             vidkit assemble broll/narration.trim.mp3 --clips-dir broll/video
                               --edit-plan broll/demo.edit-plan.json --out demo.mp4
                             then force 48 kHz, because the loudnorm chain emits 96 kHz:
                             ffmpeg -i demo.mp4 -c:v copy -c:a aac -ar 48000 -ac 1 -b:a 192k out.mp4
    demo.edit-plan.json.bak  the previous cut, kept until the video is uploaded
    video/                   the clips the plan cuts from
    preview/                 the eight Devpost gallery images, 1920x1280 (3:2), built from
                             the same clips. Regenerate rather than hand-edit.
    submission.md            every Devpost form field as a final value

A trap worth knowing about `video/40-report-long.mp4`, which is the only clip here that is a
raw screen recording rather than a Playwright capture: a Chrome find bar is on screen from 6s
to 39s and again from 43s, and the browser is windowed with the Windows taskbar visible until
it goes fullscreen at 54s. The fullscreen exit toast clears at 60s. **Only use 60s to 155s of
that clip.** Past 155s it is the rolling view with a camera permission dialog.

## Lower thirds: cards get one, footage does not

vidkit draws the lower third at a fixed `x=56, y=h-104`, which is exactly where this app puts
its own output. It was set on every segment, and it collided in fourteen of eighteen shots:
the finding rows, the take-scale columns, the SQL, and `Nothing contradicts the reference` on
every rolling shot. The worst was the catch, where the plate sat straight across
`coffee mug: reference has it right, by the laptop, frame shows left, by the laptop`, which is
the most important line in the video.

There is no fixed position that avoids this. The top band is the toolbar and the page header,
the right is the reference panel and the table's own columns, and the bottom edge is the
model's summary line with its latency and model name. That is what a full-screen app looks
like, and the app's text is the product while the plate is decoration.

So a lower third is set only on `15-source`, `05-glasses` and `50-architecture`, whose
backgrounds are surfaces I control and left empty. On footage the app already says what the
shot is in its own words, and the burned-in caption already carries the narration, so the
plate was a third text layer whose only effect was hiding the second one.

If a future cut adds a segment, leave `lower_third` empty unless the clip is a card.
