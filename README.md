# Deck TV

A 24/7 Adult Swim / Toonami-style channel running on the Steam Deck: a random
episode, then 3–5 random bumps, then another random episode, forever. It's a
real broadcast, one live stream everyone shares. Close the page, come back ten
minutes later, and you're ten minutes further along.

## Watch

| From | URL |
|---|---|
| iPhone / MacBook (Tailscale on) | http://100.94.66.74:7000 |
| Home Wi-Fi | http://192.168.1.199:7000 |
| VLC / IINA / any IPTV app | http://100.94.66.74:7000/live.m3u8 |

- **The set:** the page is an old projection TV drawn in CSS.
  - It starts muted, because browsers only autoplay muted. The green **MUTE**
    on screen and the blinking red light on the **MUTE** button (under SOUND)
    both mean no sound yet. Press **MUTE** (or the `M` key) for sound.
  - The **POWER** light is green when on air and blinks amber while tuning.
  - **VOLUME − / +** (or the ↑/↓ keys) shows a green bar of blocks on screen, like old
    TVs. There are 20 steps, and your browser remembers the level. Pressing + while muted unmutes.
  - Tap the screen to show the LIVE readout, and double-tap (or press `F`)
    for fullscreen.
  - AirPlay works from Safari.
- **▲ SURF ▼** on the base (or the `S` key) flips the channel:
  - It cuts straight to a random episode, chosen with the usual no-repeat
    rules, and joins it at a random point 10-50% of the way in.
  - You see about 2–3 seconds of static, then the new show.
  - After that the normal pattern continues.
  - It's one shared broadcast, so a surf changes the channel for everyone watching.
  - Each surf shows up in `docker compose logs` as a `SURF` line.
  - The endpoint behind it is `POST /api/surf`.
- **BUMPS ONLY** on the base (or the `B` key) switches the channel to bumps back to back:
  - Its purple light is on while the mode is on. Same static cut-in as a surf.
  - They still come from the no-repeat shuffle, and SURF skips to a new random bump.
  - Press it again to go back to regular programming, joining an episode partway in.
  - It's shared like everything else, and survives restarts (it's kept in `state/state.json`).
  - `MODE` lines in the log; the endpoints are `POST /api/bumps-only` (toggles, or
    send `{"on": true}`) and `GET /api/mode`.

## Run it

```sh
cd ~/TV
docker compose up -d           # on air (the first build takes a few minutes)
docker compose logs -f         # the ON AIR log: every bump and episode as it airs
docker compose stop            # off air
docker compose up -d           # back on air, opening with a fresh 3-5 bump break
docker compose up -d --build   # after editing anything in app/ or web/
```

It comes back by itself after a reboot, unless you stopped it.

## Adding shows and bumps

Drag files into `~/TV/shows` and `~/TV/bumps`. No restart needed.

```
shows/
  Futurama/
    Season 01/
      Futurama - S01E01 - Space Pilot 3000.mkv
  The Boondocks/
    ...
bumps/
  Toonami 2001 - Moltar.mp4        # subfolders are fine too
```

- **Shows and episodes:** every first-level folder in `shows/` is one show, and
  its episodes can sit anywhere below it.
- **Scheduling:**
  - Shows come up at equal odds and never twice in a row.
  - Within a show, every episode airs once before any repeats.
  - Bumps work the same way: no bump repeats until all of them have aired.
- **Mid-episode breaks:** if an episode file has chapter markers (most DVD rips
  do), it airs in parts with **one bump** at each act break, like the original
  commercial breaks. Chapters within 3 minutes of the start, the end, or the
  previous break are ignored (theme song, credits). A file without chapters
  plays straight through.
- **Formats:** any format ffmpeg can read works (mkv, mp4, avi, ...). Everything
  is converted live to 480p 4:3 like old Adult Swim, and widescreen gets
  letterboxed.
- **Audio:** the main English track plays and commentary tracks are skipped.
  Volume is leveled, so VHS bumps and DVD episodes sound alike.
- **Extras never air:** folders named Featurettes, Extras, Behind The Scenes,
  Deleted Scenes, Interviews, Trailers, Samples, Shorts, Scenes or Other are
  skipped.
- **New files:** a new file waits until it has sat unchanged for 30 s, so a
  half-copied episode never airs.
- **Duplicate rips** like `... - Copy.mkv` air as separate episodes, so delete them.
- **Naming for V2:** V2's "recently played" will read show, season, episode and
  title from the filename. Name episodes like `Show - S01E01 - Title.mkv`
  (Jellyfin style; `Show.S01E01.Title.avi` also works).

## How it works

- `app/tv.py` is one Python process (stdlib only) inside the container:
  1. It picks what airs next and remembers what has aired in `state/state.json`.
  2. It runs ffmpeg about 20 s ahead of air to turn each file into 2-second
     HLS segments on a RAM disk.
  3. It releases those segments into `live.m3u8` on the wall clock, so the
     channel keeps real time whether or not anyone is watching.
- `web/index.html` is the player page. Safari uses its built-in HLS player,
  and everything else uses hls.js (pinned 1.6.19).
- **Nothing to air:** if `shows/` and `bumps/` are both empty, it airs color bars.
- **Unreadable files:** a file ffmpeg can't read is logged as `SKIP` and left
  out until the next restart.
- **Settings:** every setting, bumps per break included, is a named constant
  at the top of `app/tv.py`.

## Troubleshooting

- **"Tuning in" for a few seconds after start:** normal. The stream warms up
  for about 5 s.
- **Is it on air?** Run `curl http://localhost:7000/healthz`, which answers
  `on air` or `off air`. `docker compose ps` shows healthy/unhealthy.
- **No sound:** tap the picture once.
- **An episode never airs:** check the log for `SKIP` and try playing the
  file in VLC.
- **CPU:** one ffmpeg converts SD video to 480p at a little over real time.
  `docker stats tv` shows the cost.

## V2 roadmap

- ~~**Projection-TV look**~~ **(done):** drawn in CSS instead of a PNG, so it
  stays sharp at any size.
  - It has MUTE on the front panel and SURF on the base.
  - Green readouts appear on screen, like an old TV.
- **Coming up next:**
  - the planner keeps a lookahead queue of the next few items;
  - a small `/api/status` endpoint exposes it.
- **Recently played:** the last 100 aired items are already recorded in
  `state/state.json` under `history`.
- **Show - Season - Episode title:**
  - parsed from filenames with the `SxxEyy` pattern;
  - synced to what the viewer actually sees using the playlist's
    `PROGRAM-DATE-TIME` stamps, since viewers run about 6 s behind live.
- **Later ideas:**
  - themed bump pools, e.g. Toonami bumps around anime and Adult Swim bumps
    around comedy;
  - HTTPS via `tailscale serve`;
  - skipping duplicate rips automatically.
