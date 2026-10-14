#!/usr/bin/env python3
"""Deck TV: a 24/7 Adult Swim / Toonami-style channel.

Airs a break of 3-5 random bumps, then a random episode, then another break,
forever, as one live HLS stream that every viewer shares. It is a broadcast,
not video on demand: time passes whether or not anyone is watching, so leaving
for ten minutes and coming back puts you ten minutes later.

    /media/shows/<Show>/**/<episode>      /media/bumps/**/<bump>

How it hangs together:
  programme()  picks what airs next (shuffle bags, persisted in /state)
  encoder      one ffmpeg per item turns any source into 640x480 4:3 H.264/AAC,
               cut into 2 s MPEG-TS segments on /hls (a tmpfs), ~20 s ahead of air
  publisher    releases those segments into live.m3u8 on the wall clock, so the
               stream stays locked to real time however fast ffmpeg runs
  http         serves the page, the playlist and the segments

Stdlib only; ffmpeg and ffprobe come from Debian.
"""

import collections
import itertools
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# --- where things live (container paths; see compose.yaml) -------------------
SHOWS_DIR = "/media/shows"
BUMPS_DIR = "/media/bumps"
STATE_FILE = "/state/state.json"
HLS_DIR = "/hls"
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PORT = 8000

# --- programming --------------------------------------------------------------
BUMPS_MIN, BUMPS_MAX = 3, 5  # bumps per break
SETTLE_SECONDS = 30   # skip files modified this recently: probably still copying
BREAK_MARGIN = 180    # mid-episode breaks: none within 3 min of either end or of each other
HISTORY_MAX = 100
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".mpg", ".mpeg", ".ts",
              ".m2ts", ".vob", ".wmv", ".flv", ".webm"}
# Jellyfin-style extras folders. DVD rips keep featurettes inside the season
# folders, and nobody wants an animatic airing in prime time.
EXTRAS_DIRS = {"featurettes", "extras", "behind the scenes", "deleted scenes",
               "interviews", "trailers", "samples", "shorts", "scenes", "other"}
SKIP_AUDIO = re.compile(r"commentary|director|descri", re.I)

# --- picture and stream -------------------------------------------------------
WIDTH, HEIGHT = 640, 480  # 4:3 SD like old Adult Swim; every source is forced into it
FPS = "30000/1001"
SEGMENT = 2               # seconds per segment, and the keyframe interval
TARGET_DURATION = 3       # fixed for the stream's life; >= every rounded EXTINF
WINDOW_SEGMENTS = 6       # Apple: a live playlist always holds >= 6 segments...
WINDOW_SECONDS = 20.0     # ...and well over 3x the target duration
LEAD_SECONDS = 20.0       # stay this far ahead of air before starting the next item
RETIRE_SECONDS = 60.0     # keep a segment file this long after it leaves the playlist
READRATE = "1.1"          # ffmpeg reads a little faster than real time...
READRATE_BURST = "10"     # ...after taking each item's first 10 s at full speed
SURF_BURST = 12.0         # a channel surf puts this much of the new episode on air at once...
SURF_READRATE_BURST = "20"  # ...so encode enough of it at full speed to cover that
AUDIO_FILTER = ("aresample=async=1:first_pts=0,aformat=channel_layouts=stereo,"
                "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,apad")
SEG_PATH = re.compile(r"/seg/(\d+-\d{5})/(\d{5}\.ts)")


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def iso_utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- library --------------------------------------------------------------------
def _videos(root: str) -> list[str]:
    """Video files under root, relative to it, skipping extras and half-copied files."""
    found = []
    settled = time.time() - SETTLE_SECONDS
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d.lower() not in EXTRAS_DIRS]
        for name in filenames:
            if name.startswith(".") or os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.stat(path).st_mtime > settled:
                    continue
            except OSError:
                continue
            found.append(os.path.relpath(path, root))
    return sorted(found)


def scan(bad: set[str]) -> tuple[dict[str, list[str]], list[str]]:
    """({show: [episode, ...]}, [bump, ...]), minus files ffmpeg already choked on."""
    shows = {}
    try:
        names = sorted(os.listdir(SHOWS_DIR))
    except OSError:
        names = []
    for name in names:
        root = os.path.join(SHOWS_DIR, name)
        if name.startswith(".") or not os.path.isdir(root):
            continue
        eps = [e for e in _videos(root) if os.path.join(root, e) not in bad]
        if eps:
            shows[name] = eps
    bumps = [b for b in _videos(BUMPS_DIR) if os.path.join(BUMPS_DIR, b) not in bad]
    return shows, bumps


# --- programming ------------------------------------------------------------------
class State:
    """What has aired, persisted so the no-repeat rules survive a restart."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path) as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            old = {}
        self.data = {"last_show": None, "played": {}, "bumps_played": [], "history": [],
                     "bumps_only": False, **old}

    def save(self) -> None:
        """Atomic rewrite. The caller holds self.lock."""
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, indent=1)
        os.replace(tmp, self.path)


@dataclass
class Item:
    kind: str                   # "bump", "episode" or "bars"
    path: str | None = None     # absolute, inside the container
    show: str | None = None
    rel: str | None = None      # relative to the show folder, or to bumps/
    start: float | None = None  # seconds into the file; None = top / end
    end: float | None = None
    part: int = 1
    parts: int = 1
    surf: int = 0               # the channel-surf request this item answers, or 0
    info: dict | None = None    # probe() output, if already probed
    burst: float = SURF_BURST   # seconds a surf puts on air at once (less for a short bump)
    id: str = ""                # "<session>-<n>", set by the encoder; names its /hls dir

    def __str__(self) -> str:
        if self.kind == "episode":
            notes = [f"part {self.part}/{self.parts}"] if self.parts > 1 else []
            if self.start:
                notes.append("from %d:%02d" % divmod(int(self.start), 60))
            if self.surf:
                notes.append("channel surf")
            return f"{self.show} — {self.rel}" + (f" ({', '.join(notes)})" if notes else "")
        return self.rel or "color bars"


def pick_bumps(state: State, bumps: list[str], count: int | None = None) -> list[str]:
    """`count` (default 3-5) distinct bumps from a bag that empties before any bump repeats."""
    if not bumps:
        return []
    want = min(count or random.randint(BUMPS_MIN, BUMPS_MAX), len(bumps))
    with state.lock:
        known = set(bumps)
        played = [b for b in state.data["bumps_played"] if b in known]
        taken = set(played)
        chosen: list[str] = []
        while len(chosen) < want:
            fresh = [b for b in bumps if b not in taken]
            if not fresh:  # bag empty: refill it, minus what this break already has
                played, taken = [], set(chosen)
                fresh = [b for b in bumps if b not in taken]
            pick = random.choice(fresh)
            chosen.append(pick)
            played.append(pick)
            taken.add(pick)
        state.data["bumps_played"] = played
        state.save()
    return chosen


def pick_episode(state: State, shows: dict[str, list[str]]) -> tuple[str, str]:
    """A show at equal odds (never the previous one), then an unaired episode of it."""
    with state.lock:
        last = state.data["last_show"]
        show = random.choice([s for s in shows if s != last] or list(shows))
        eps = shows[show]
        known = set(eps)
        played = [e for e in state.data["played"].get(show, []) if e in known]
        seen = set(played)
        fresh = [e for e in eps if e not in seen]
        if not fresh:  # every episode has aired: new bag, but don't open with the last one
            fresh = [e for e in eps if not played or e != played[-1]] or eps
            played = []
        ep = random.choice(fresh)
        state.data["played"][show] = played + [ep]
        state.data["last_show"] = show
        state.save()
    return show, ep


def probe(path: str) -> dict:
    """ffprobe's streams, chapters and duration of a file; {} if it can't read it."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-of", "json", "-show_streams",
                              "-show_chapters", "-show_entries", "format=duration", path],
                             capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout) if out.returncode == 0 else {}
    except (subprocess.TimeoutExpired, ValueError):
        return {}


def duration(info: dict) -> float:
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


def breaks(length: float, starts: list[float], begin: float = 0.0) -> list[float]:
    """Where to cut away mid-episode: the file's own chapter marks, if it has any.

    DVD chapters usually sit on the original act breaks. Marks within 3 min of
    where we joined, the end (credits), or the previous break are ignored, and
    a file without chapters plays straight through.
    """
    cuts: list[float] = []
    for t in starts:
        if t - (cuts[-1] if cuts else begin) >= BREAK_MARGIN and t <= length - BREAK_MARGIN:
            cuts.append(t)
    return cuts


def episode(state: State, bad: set[str], shows: dict[str, list[str]], bumps: list[str], surf: int):
    """One episode, in parts at its chapter marks with one bump between them,
    like the commercial breaks it was cut for. A channel surf joins it at a
    random point 10-50% of the way in.

    The encoder sends back True when an item produced nothing, so a broken
    episode gets replaced by another episode instead of leaving two breaks
    back to back.
    """
    while shows:
        show, rel = pick_episode(state, shows)
        path = os.path.join(SHOWS_DIR, show, rel)
        info = probe(path)
        length = duration(info)
        starts = sorted(float(c["start_time"]) for c in info.get("chapters", []))
        begin = random.uniform(length * 0.10, length * 0.50) if surf and length else None
        edges = [begin, *(breaks(length, starts, begin or 0.0) if bumps else []), None]
        parts = len(edges) - 1
        for i in range(parts):
            if i:  # mid-episode commercial break: one bump
                for b in pick_bumps(state, bumps, 1):
                    yield bump_item(b)
            failed = yield Item("episode", path, show=show, rel=rel, start=edges[i],
                                end=edges[i + 1], part=i + 1, parts=parts,
                                surf=surf if i == 0 else 0, info=info)
            if failed:
                break
        if not (failed and i == 0):  # it aired, at least in part
            return
        shows, _ = scan(bad)  # part 1 failed, and the file is in `bad` now


def bump_item(rel: str, surf: int = 0) -> Item:
    item = Item("bump", os.path.join(BUMPS_DIR, rel), rel=rel, surf=surf)
    if surf:  # a surf lands on this bump: burst no more of it than it has
        item.info = probe(item.path)
        item.burst = min(SURF_BURST, max(0.0, duration(item.info) - SEGMENT))
    return item


def programme(state: State, bad: set[str], surf: int = 0):
    """Items forever: a break of bumps, an episode, a break, an episode...

    Every session opens with a break. A channel surf (surf = its request
    number) opens instead straight on an episode, joined mid-show.
    In bumps-only mode it's bumps back to back, and a surf skips to a new one.
    """
    while True:
        shows, bumps = scan(bad)
        if not shows and not bumps:
            yield Item("bars")
            continue
        if bumps and (state.data["bumps_only"] or not shows):
            for i, rel in enumerate(pick_bumps(state, bumps)):
                yield bump_item(rel, surf if i == 0 else 0)
            surf = 0
            continue
        if not surf:
            for rel in pick_bumps(state, bumps):
                yield bump_item(rel)
            shows, bumps = scan(bad)  # the break took a while; pick up anything new
        yield from episode(state, bad, shows, bumps, surf)
        surf = 0


# --- encoding ---------------------------------------------------------------------
def pick_audio(streams: list[dict]) -> int | None:
    """Index among the audio streams: the main English track, never commentary."""
    audio = [{k.lower(): v for k, v in s.get("tags", {}).items()}
             for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        return None
    main = [i for i, tags in enumerate(audio) if not SKIP_AUDIO.search(tags.get("title", ""))]
    main = main or list(range(len(audio)))
    eng = [i for i in main if audio[i].get("language", "").lower() in ("eng", "en")]
    return (eng or main)[0]


def video_filter(stream: dict) -> str:
    """Any source -> 640x480 4:3, square pixels, 29.97 fps. Downscales first, in one step."""
    w, h = int(stream["width"]), int(stream["height"])
    num, _, den = (stream.get("sample_aspect_ratio") or "1:1").partition(":")
    try:
        sar = int(num) / int(den)
    except (ValueError, ZeroDivisionError):
        sar = 0.0
    dar = w * (sar if sar > 0 else 1.0) / h
    if dar > WIDTH / HEIGHT:  # wider than 4:3: letterbox
        ow, oh = WIDTH, round(WIDTH / dar / 2) * 2
    else:                     # 4:3 or narrower: pillarbox
        ow, oh = round(HEIGHT * dar / 2) * 2, HEIGHT
    ow, oh = max(2, min(WIDTH, ow)), max(2, min(HEIGHT, oh))
    chain = []
    # DVD and VHS sources are often interlaced. deint=interlaced only touches
    # frames flagged as interlaced, so progressive SD passes straight through.
    if h <= 576 or stream.get("field_order") in ("tt", "bb", "tb", "bt"):
        chain.append("bwdif=deint=interlaced")
    chain += [f"scale={ow}:{oh}", "setsar=1", f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2",
              f"fps={FPS}", "format=yuv420p"]
    return ",".join(chain)


def ffmpeg_cmd(item: Item, outdir: str) -> list[str] | None:
    cmd = ["nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if item.kind == "bars":
        cmd += ["-f", "lavfi", "-i", f"smptebars=size={WIDTH}x{HEIGHT}:rate={FPS}",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-map", "0:v", "-map", "1:a", "-t", "10", "-vf", "format=yuv420p"]
    else:
        streams = (item.info or probe(item.path)).get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"
                      and not s.get("disposition", {}).get("attached_pic")), None)
        if video is None:
            return None
        audio = pick_audio(streams)
        cmd += ["-readrate", READRATE, "-readrate_initial_burst",
                SURF_READRATE_BURST if item.surf else READRATE_BURST]
        if item.start:  # a later part of a chaptered episode
            cmd += ["-ss", f"{item.start:.3f}"]
        if item.end:
            cmd += ["-t", f"{item.end - (item.start or 0):.3f}"]
        cmd += ["-i", item.path]
        if audio is None:  # silent source: give it a silent track so every item has audio
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        cmd += ["-map", "0:V:0", "-map", "1:a:0" if audio is None else f"0:a:{audio}",
                "-vf", video_filter(video), "-af", AUDIO_FILTER, "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main", "-crf", "21",
            "-maxrate", "1800k", "-bufsize", "3600k",
            "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT})", "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-f", "segment", "-segment_format", "mpegts", "-segment_time", str(SEGMENT),
            "-segment_time_delta", "0.0167",
            # One TS muxer across all of an item's segments, so continuity
            # counters don't restart each file (Safari may drop those packets).
            # PAT/PMT are still re-sent at the start of every segment.
            "-individual_header_trailer", "0",
            # One "name,start,end" line per finished segment, flushed as it closes.
            "-segment_list", "pipe:1", "-segment_list_type", "csv",
            os.path.join(outdir, "%05d.ts")]
    return cmd


@dataclass(slots=True)
class Segment:
    item: Item
    name: str
    dur: float
    offset: float       # seconds of this item before this segment
    ready_at: float = field(default_factory=time.time)
    seq: int = 0
    cc: int = 0
    start: float = 0.0  # wall-clock time it went on air
    pdt: float = 0.0    # its PROGRAM-DATE-TIME: start, but never running backwards

    @property
    def first(self) -> bool:
        return self.offset == 0

    @property
    def path(self) -> str:
        return os.path.join(HLS_DIR, self.item.id, self.name)


class Channel:
    def __init__(self, state: State):
        self.state = state
        self.lock = threading.Lock()
        self.ready: collections.deque[Segment] = collections.deque()   # encoded, not aired yet
        self.window: collections.deque[Segment] = collections.deque()  # in the playlist now
        self.retired: collections.deque[tuple[float, Segment]] = collections.deque()
        # Sequence numbers start from the clock, so they keep climbing across
        # restarts instead of going backwards on a player that stayed open.
        self.session = int(time.time() * 10)
        self.next_seq = self.session
        self.cc = self.session - 1  # discontinuity number; +1 per item
        self.last_end = 0.0         # when the last published segment finished airing
        self.pdt_end = 0.0          # where the last PROGRAM-DATE-TIME label ended
        self.last_publish = 0.0
        self.playlist = b""
        self.proc: subprocess.Popen | None = None  # the ffmpeg encoding right now
        self.changed = threading.Condition(self.lock)
        self.surf_asked = 0         # channel-surf requests so far...
        self.surf_aired = 0         # ...and the latest one now on air
        self.surf_info: dict = {}

    def lead(self) -> float:
        with self.lock:
            return sum(s.dur for s in self.ready)

    def surf(self, timeout: float = 25.0) -> dict | None:
        """Cut to a random episode mid-show, for every viewer. Returns once it's on air."""
        with self.lock:
            self.surf_asked += 1
            ask = self.surf_asked
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()  # the encoder notices, flushes and starts the surf
            if not self.changed.wait_for(lambda: self.surf_aired >= ask, timeout):
                return None
            return dict(self.surf_info)

    def flush(self) -> None:
        """Drop everything encoded but not yet on air, files included."""
        with self.lock:
            self.ready.clear()
            keep = {s.path for s in self.window} | {s.path for _, s in self.retired}
        for name in os.listdir(HLS_DIR):
            folder = os.path.join(HLS_DIR, name)
            try:
                for f in os.listdir(folder):
                    if os.path.join(folder, f) not in keep:
                        os.remove(os.path.join(folder, f))
                os.rmdir(folder)  # only succeeds if nothing in it is on air
            except OSError:
                pass


def encoder(ch: Channel) -> None:
    bad: set[str] = set()
    items = programme(ch.state, bad)
    failed = None
    handled = 0  # the last channel-surf request acted on
    for n in itertools.count(1):
        while ch.lead() >= LEAD_SECONDS and ch.surf_asked == handled:
            time.sleep(0.25)
        if ch.surf_asked != handled:  # someone hit Channel Surf: drop what's queued, cut over
            handled = ch.surf_asked
            ch.flush()
            items, failed = programme(ch.state, bad, surf=handled), None
        item = items.send(failed)
        item.id = f"{ch.session}-{n:05d}"
        outdir = os.path.join(HLS_DIR, item.id)
        os.makedirs(outdir)
        count, offset = 0, 0.0
        cmd = ffmpeg_cmd(item, outdir)
        if cmd is not None:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
            with ch.lock:
                ch.proc = proc
                if ch.surf_asked != handled:  # a surf landed while this was starting
                    proc.terminate()
            for line in proc.stdout:
                try:
                    name, start, end = line.strip().rsplit(",", 2)
                    dur = float(end) - float(start)
                except ValueError:
                    continue
                if dur > 0:
                    with ch.lock:
                        ch.ready.append(Segment(item, os.path.basename(name), dur, offset))
                    count, offset = count + 1, offset + dur
            proc.wait()
            with ch.lock:
                ch.proc = None
            if ch.surf_asked != handled:
                continue  # cut off by a surf, not broken
            if count and proc.returncode:
                log(f"WARN    ffmpeg exit {proc.returncode} partway through {item}")
        failed = count == 0
        if failed:
            log(f"SKIP    can't play {item}")
            if item.path:
                bad.add(item.path)
            shutil.rmtree(outdir, ignore_errors=True)
            time.sleep(1)  # don't spin if something is badly wrong


# --- publishing ---------------------------------------------------------------------
def render(window: collections.deque[Segment]) -> bytes:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3",
             f"#EXT-X-TARGETDURATION:{TARGET_DURATION}",
             f"#EXT-X-MEDIA-SEQUENCE:{window[0].seq}",
             f"#EXT-X-DISCONTINUITY-SEQUENCE:{window[0].cc}"]
    prev = None
    for s in window:
        if prev is not None and s.cc != prev.cc:
            lines.append("#EXT-X-DISCONTINUITY")
        lines += [f"#EXT-X-PROGRAM-DATE-TIME:{iso_utc(s.pdt)}",
                  f"#EXTINF:{s.dur:.3f},",
                  f"seg/{s.item.id}/{s.name}"]
        prev = s
    return ("\n".join(lines) + "\n").encode()


def on_air(ch: Channel, seg: Segment) -> None:
    item = seg.item
    log(f"ON AIR  {item.kind:<8}{item}")
    if item.kind == "bars" or item.part > 1:  # history gets each episode once
        return
    entry = {"aired_at": datetime.fromtimestamp(seg.start).astimezone().isoformat(timespec="seconds"),
             "kind": item.kind, "show": item.show, "file": item.rel}
    with ch.state.lock:
        history = ch.state.data["history"]
        history.append(entry)
        del history[:-HISTORY_MAX]
        ch.state.save()


def publisher(ch: Channel) -> None:
    """Put each segment in the playlist once it has aired, on the wall clock."""
    while True:
        time.sleep(0.1)
        now = time.time()
        aired = None
        with ch.lock:
            if ch.ready:
                seg = ch.ready[0]
                burst = bool(seg.item.surf) and seg.offset < seg.item.burst
                if burst:
                    # A channel surf cuts in: its opening seconds count as already
                    # aired, so they go out at once and the rest follows at normal
                    # pace right behind them. A player reloading now lands on the
                    # new show with its usual cushion behind the live edge.
                    if seg.first:
                        ch.last_end = now - seg.item.burst
                    start = ch.last_end
                else:
                    # A late segment re-anchors the clock rather than bursting to catch up.
                    start = max(ch.last_end, seg.ready_at)
                    if now - (start + seg.dur) > 5:  # woke from a freeze (docker compose pause)
                        start = now - seg.dur
                if now >= start + seg.dur:
                    ch.ready.popleft()
                    if seg.first:
                        ch.cc += 1
                        aired = seg
                    seg.seq, seg.cc, seg.start = ch.next_seq, ch.cc, start
                    seg.pdt = max(start, ch.pdt_end)  # a surf rewinds the clock; the labels don't
                    ch.pdt_end = seg.pdt + seg.dur
                    ch.next_seq += 1
                    ch.window.append(seg)
                    ch.last_end = start + seg.dur
                    ch.last_publish = now
                    total = sum(s.dur for s in ch.window)
                    while (len(ch.window) > WINDOW_SEGMENTS
                           and total - ch.window[0].dur >= WINDOW_SECONDS):
                        old = ch.window.popleft()
                        total -= old.dur
                        ch.retired.append((now, old))
                    ch.playlist = render(ch.window)
                    if burst and seg.offset + seg.dur >= seg.item.burst:  # the surf is on air
                        ch.surf_aired = max(ch.surf_aired, seg.item.surf)
                        ch.surf_info = {"show": seg.item.show, "file": seg.item.rel,
                                        "from": round(seg.item.start or 0)}
                        ch.changed.notify_all()
            expired = []
            while ch.retired and now - ch.retired[0][0] > RETIRE_SECONDS:
                expired.append(ch.retired.popleft()[1])
        for old in expired:
            try:
                os.remove(old.path)
                os.rmdir(os.path.dirname(old.path))  # only succeeds after the item's last segment
            except OSError:
                pass
        if aired:
            on_air(ch, aired)


# --- http ---------------------------------------------------------------------------
def make_handler(ch: Channel):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "DeckTV"

        def log_message(self, *args) -> None:
            pass  # every viewer asks for something every 2 s; the log is for ON AIR lines

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            path = urlsplit(self.path).path
            if path == "/api/surf":
                log(f"SURF    requested from {self.client_address[0]}")
            elif path == "/api/bumps-only":
                try:
                    on = bool(json.loads(raw)["on"])
                except (ValueError, KeyError, TypeError):
                    on = not ch.state.data["bumps_only"]  # no body: toggle
                with ch.state.lock:
                    ch.state.data["bumps_only"] = on
                    ch.state.save()
                log(f"MODE    bumps only {'ON' if on else 'OFF'}, from {self.client_address[0]}")
            else:
                return self.send(404, b"not found\n", "text/plain", "no-store")
            info = ch.surf()  # blocks until the new programme is on air
            if info is None:
                return self.send(504, b'{"error": "surf timed out"}\n', "application/json", "no-store")
            info["bumps_only"] = ch.state.data["bumps_only"]
            self.send(200, json.dumps(info).encode(), "application/json", "no-store")

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                return self.send_file("index.html", "text/html; charset=utf-8", "no-cache")
            if path == "/hls.min.js":
                return self.send_file("hls.min.js", "text/javascript", "max-age=86400")
            if path == "/live.m3u8":
                with ch.lock:
                    body = ch.playlist if len(ch.window) >= 3 else None
                if body is None:
                    return self.send(503, b"warming up\n", "text/plain", "no-store",
                             {"Retry-After": "2"})
                return self.send(200, body, "application/vnd.apple.mpegurl", "no-cache, no-store")
            m = SEG_PATH.fullmatch(path)
            if m:
                try:
                    with open(os.path.join(HLS_DIR, m[1], m[2]), "rb") as fh:
                        body = fh.read()
                except OSError:
                    return self.send(404, b"gone\n", "text/plain", "no-store")
                return self.send(200, body, "video/mp2t", "max-age=300")
            if path == "/api/mode":
                body = json.dumps({"bumps_only": ch.state.data["bumps_only"]}).encode()
                return self.send(200, body, "application/json", "no-store")
            if path == "/healthz":
                ok = time.time() - ch.last_publish < 10
                return self.send(200 if ok else 503, b"on air\n" if ok else b"off air\n",
                                 "text/plain", "no-store")
            self.send(404, b"not found\n", "text/plain", "no-store")

        def send_file(self, name: str, ctype: str, cache: str) -> None:
            try:
                with open(os.path.join(WEB_DIR, name), "rb") as fh:
                    body = fh.read()
            except OSError:
                return self.send(404, b"not found\n", "text/plain", "no-store")
            self.send(200, body, ctype, cache)

        def send(self, code: int, body: bytes, ctype: str, cache: str,
                 extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("Access-Control-Allow-Origin", "*")  # lets web players elsewhere tune in
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        # Phones drop connections mid-segment all the time; that isn't news.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def run_thread(target, *args) -> None:
    """A dead encoder or publisher is a dead channel: exit so Docker restarts it."""
    def body() -> None:
        try:
            target(*args)
        except BaseException:
            traceback.print_exc()
            os._exit(1)
    threading.Thread(target=body, name=target.__name__, daemon=True).start()


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 ignores SIGTERM unless handled
    os.makedirs(HLS_DIR, exist_ok=True)
    for name in os.listdir(HLS_DIR):  # leftovers if the process restarted in place
        shutil.rmtree(os.path.join(HLS_DIR, name), ignore_errors=True)
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    ch = Channel(State(STATE_FILE))
    log(f"SESSION start: {BUMPS_MIN}-{BUMPS_MAX} bumps per break, {WIDTH}x{HEIGHT}, "
        f"serving on :{PORT}")
    run_thread(encoder, ch)
    run_thread(publisher, ch)
    Server(("", PORT), make_handler(ch)).serve_forever()


if __name__ == "__main__":
    main()
