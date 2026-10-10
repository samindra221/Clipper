#!/usr/bin/env python3
"""
clipper - a small, local, lightweight video clipping tool.

Works on a YouTube link or a local video file.

  clip   cut clips from given timestamps
  auto   "AI" highlight finder: picks the best moments automatically
         (YouTube "Most replayed" heatmap, or audio loudness as a fallback)

Needs: python 3.8+, ffmpeg, yt-dlp (pip install yt-dlp). Nothing else.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class ClipperError(Exception):
    """Raised for any user-facing failure (CLI prints it, web UI shows it)."""


def die(msg):
    raise ClipperError(msg)


def log(msg):
    """Progress output. The web UI replaces this to capture messages."""
    print(msg)


def is_url(s):
    return bool(re.match(r"^https?://", s.strip(), re.I))


def parse_time(s):
    """'90' | '1:30' | '01:02:03' | '1h2m3s' | '1m30s' -> seconds (float)."""
    s = s.strip().lower()
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?", s)
    if m and any(m.groups()) and re.search(r"[hms]", s):
        h, mi, se = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        die(f"can't read time '{s}' (use 90, 1:30, 01:02:03 or 1m30s)")
    if not 1 <= len(parts) <= 3:
        die(f"can't read time '{s}'")
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def fmt_time(t, sep=":"):
    t = int(round(t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}{sep}{m:02d}{sep}{s:02d}"


def parse_range(text):
    """'1:30-2:10 optional label' -> (start, end, label)."""
    text = text.strip()
    m = re.match(r"^(\S+?)\s*(?:-|–|to|\s)\s*(\S+?)(?:\s+(.*))?$", text)
    if not m:
        die(f"can't read range '{text}' (expected START-END, e.g. 1:30-2:10)")
    start, end = parse_time(m.group(1)), parse_time(m.group(2))
    if end <= start:
        die(f"range '{text}': end must be after start")
    return start, end, (m.group(3) or "").strip()


def safe_name(s, maxlen=60):
    s = re.sub(r"[^\w\-. ]+", "", s).strip().replace(" ", "_")
    return s[:maxlen] or "clip"


def need(binary):
    if not shutil.which(binary):
        die(f"'{binary}' not found. Install it and make sure it's on your PATH.")


def ytdlp_cmd():
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        die("yt-dlp not found. Install it with: pip install yt-dlp")


def run(cmd, quiet=True):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        tail = "\n".join(r.stderr.strip().splitlines()[-6:])
        die(f"command failed: {cmd[0]}\n{tail}")
    return r.stdout


# --------------------------------------------------------------------------
# source info
# --------------------------------------------------------------------------

def youtube_info(url):
    out = run(ytdlp_cmd() + ["-J", "--no-playlist", "--no-warnings", url])
    return json.loads(out)


def local_duration(path):
    need("ffprobe")
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


# --------------------------------------------------------------------------
# cutting
# --------------------------------------------------------------------------

def vertical_filter(mode):
    if mode == "crop":      # fill the 9:16 frame, trim the sides
        return "crop='min(iw,ih*9/16)':ih,scale=1080:1920:flags=lanczos,setsar=1"
    # "blur": whole video centred over a blurred copy of itself
    return ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=25:5[bg];"
            "[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")


def cut_local(src, start, end, out, args):
    """Cut [start,end] from a local file with ffmpeg."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", src]
    if args.fast and not args.vertical:
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]   # instant, keyframe-accurate
    else:
        if args.vertical:
            cmd += ["-filter_complex" if args.vertical == "blur" else "-vf", vertical_filter(args.vertical)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf),
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
    cmd.append(out)
    run(cmd)


def cut_youtube(url, start, end, out, args):
    """Download ONLY the requested section from YouTube, then post-process."""
    tmpdir = tempfile.mkdtemp(prefix="clipper_")
    try:
        fmt = f"bv*[height<={args.quality}]+ba/b[height<={args.quality}]/b"
        tmpl = os.path.join(tmpdir, "part.%(ext)s")
        run(ytdlp_cmd() + [
            "--no-playlist", "--no-warnings", "-q",
            "--download-sections", f"*{start:.2f}-{end:.2f}",
            "--force-keyframes-at-cuts",
            "-f", fmt, "--merge-output-format", "mp4", "-o", tmpl, url])
        parts = [f for f in os.listdir(tmpdir) if f.startswith("part.")]
        if not parts:
            die("download produced no file")
        part = os.path.join(tmpdir, parts[0])
        if args.vertical:
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", part,
                   "-filter_complex" if args.vertical == "blur" else "-vf", vertical_filter(args.vertical),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf),
                   "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out]
            run(cmd)
        else:
            shutil.move(part, out)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def make_clip(src, start, end, label, idx, title, args):
    os.makedirs(args.out, exist_ok=True)
    base = safe_name(label or title or "clip")
    name = f"{idx:02d}_{base}_{fmt_time(start, '')}-{fmt_time(end, '')}.mp4"
    out = os.path.join(args.out, name)
    log(f"  [{idx}] {fmt_time(start)} -> {fmt_time(end)}  ({end - start:.0f}s)  {label}")
    if is_url(src):
        cut_youtube(src, start, end, out, args)
    else:
        cut_local(src, start, end, out, args)
    log(f"      saved: {out}")
    return out


# --------------------------------------------------------------------------
# "AI" highlight detection
# --------------------------------------------------------------------------

def heatmap_scores(info, duration):
    """YouTube 'Most replayed' graph -> one score per second (or None)."""
    hm = info.get("heatmap")
    if not hm:
        return None
    n = int(duration) + 1
    scores = [0.0] * n
    for seg in hm:
        for s in range(int(seg["start_time"]), min(n, int(seg["end_time"]) + 1)):
            scores[s] = seg["value"]
    return scores


def audio_scores(src, args):
    """Loudness per second using ffmpeg's astats. No numpy needed."""
    need("ffmpeg")
    path, tmp = src, None
    if is_url(src):
        log("  downloading audio for analysis...")
        tmp = tempfile.mkdtemp(prefix="clipper_")
        run(ytdlp_cmd() + ["--no-playlist", "--no-warnings", "-q", "-f", "ba",
                           "-o", os.path.join(tmp, "a.%(ext)s"), src])
        path = os.path.join(tmp, os.listdir(tmp)[0])
    try:
        cmd = ["ffmpeg", "-v", "error", "-i", path, "-vn",
               "-af", "aresample=8000,aformat=channel_layouts=mono,"
                      "asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,"
                      "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
               "-f", "null", "-"]
        out = run(cmd)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    scores = []
    for line in out.splitlines():
        if "RMS_level=" in line:
            v = line.split("=", 1)[1]
            scores.append(-90.0 if v in ("-inf", "inf", "nan") else max(-90.0, float(v)))
    if not scores:
        die("couldn't analyse audio (does the video have sound?)")
    lo = min(scores)
    return [s - lo for s in scores]          # shift so quiet = 0


def pick_windows(scores, length, count, min_gap):
    """Best non-overlapping windows by average score."""
    n = len(scores)
    length = min(length, n)
    pre = [0.0]
    for s in scores:
        pre.append(pre[-1] + s)
    cands = sorted(((pre[i + length] - pre[i]) / length, i) for i in range(n - length + 1))[::-1]
    chosen = []
    for _, i in cands:
        if all(i + length + min_gap <= c or c + length + min_gap <= i for c in chosen):
            chosen.append(i)
            if len(chosen) == count:
                break
    return sorted(chosen)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_clip(args):
    need("ffmpeg")
    ranges = [parse_range(r) for r in (args.ts or [])]
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0].strip()
                if line:
                    ranges.append(parse_range(line))
    if not ranges:
        die("give at least one range, e.g.  -t 1:30-2:10   or   --file timestamps.txt")
    title = ""
    if is_url(args.source):
        title = youtube_info(args.source).get("title", "")
    log(f"Cutting {len(ranges)} clip(s)...")
    for i, (s, e, label) in enumerate(ranges, 1):
        make_clip(args.source, s, e, label, i, title, args)
    log("Done.")


def find_highlights(source, length, count, gap, args=None):
    """Return (title, method, [(start, end), ...]) for the best moments."""
    need("ffmpeg")
    title = ""
    if is_url(source):
        info = youtube_info(source)
        title, duration = info.get("title", ""), info.get("duration") or 0
        scores = heatmap_scores(info, duration)
        method = "YouTube 'Most replayed' heatmap"
        if scores is None:
            scores, method = audio_scores(source, args), "audio loudness"
    else:
        scores, method = audio_scores(source, args), "audio loudness"
    starts = pick_windows(scores, length, count, gap)
    return title, method, [(s, min(s + length, len(scores))) for s in starts]


def cmd_auto(args):
    title, method, spans = find_highlights(args.source, args.length, args.count, args.gap, args)
    log(f"Found {len(spans)} highlight(s) using {method}.")
    for i, (s, e) in enumerate(spans, 1):
        if args.dry_run:
            log(f"  [{i}] {fmt_time(s)} -> {fmt_time(e)}")
        else:
            make_clip(args.source, s, e, f"highlight{i}", i, title, args)
    log("Done." if not args.dry_run else "(dry run - nothing cut. Edit and use `clip` if you like.)")


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("source", help="YouTube link or path to a local video")
    common.add_argument("-o", "--out", default="clips", help="output folder (default: clips)")
    common.add_argument("--vertical", nargs="?", const="crop", choices=["crop", "blur"],
                        help="export 9:16 for Reels/Shorts/TikTok: 'crop' (default) or 'blur' background")
    common.add_argument("-q", "--quality", type=int, default=720, help="max YouTube height (default 720)")
    common.add_argument("--crf", type=int, default=20, help="x264 quality, lower = better (default 20)")
    common.add_argument("--fast", action="store_true",
                        help="local files only: stream-copy, instant, but cuts snap to keyframes")

    p = argparse.ArgumentParser(prog="clipper", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("clip", parents=[common], help="cut clips from timestamps")
    c.add_argument("-t", "--ts", action="append", metavar="START-END[ label]",
                   help="range like 1:30-2:10 (repeatable). Label optional: \"1:30-2:10 funny moment\"")
    c.add_argument("-f", "--file", help="text file, one range per line (# for comments)")
    c.set_defaults(func=cmd_clip)

    a = sub.add_parser("auto", parents=[common], help="find the best moments automatically")
    a.add_argument("-n", "--count", type=int, default=3, help="how many clips (default 3)")
    a.add_argument("-l", "--length", type=int, default=30, help="clip length in seconds (default 30)")
    a.add_argument("--gap", type=int, default=10, help="min seconds between clips (default 10)")
    a.add_argument("--dry-run", action="store_true", help="only print the timestamps found")
    a.set_defaults(func=cmd_auto)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except ClipperError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
