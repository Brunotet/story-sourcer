#!/usr/bin/env python3
"""
Video analyzer: downloads short videos (Facebook reels, YouTube Shorts, TikTok...), transcribes the
narration, finds scene cuts, saves keyframes + a contact sheet, and OCRs any on-screen text.

For every URL it writes   <out>/<label>/<NN>/   with:
    meta.json        title, caption, uploader, views/likes, duration, resolution
    transcript.json  timestamped segments + full text + words-per-minute
    transcript.txt   readable transcript with [mm:ss] stamps
    shots.json       scene-cut times, shot count, average shot length
    frames/*.jpg     keyframes at the scene cuts (small, 540 px wide)
    sheet.jpg        contact sheet of all keyframes with timestamps
    ocr.json         on-screen text found in each keyframe
    summary.md       one-page summary of the above
and   <out>/<label>/index.json   listing every URL with ok/error.

A failure on one URL never stops the others. The video file itself is deleted, only the analysis stays.
Optional: pass --cookies <cookies.txt> (Netscape format) if a site demands a login.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

MAX_FRAMES = 18
MIN_FRAMES = 6
MIN_SPACING = 1.0
SCENE_THRESHOLD = 0.30
_MODEL = None


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def parse_urls(raw):
    return [u.strip() for u in re.split(r"[\s,]+", raw or "") if u.strip().startswith("http")]


def download(url, work, cookies):
    base = ["yt-dlp", "--no-playlist", "--write-info-json", "--no-progress", "--retries", "3",
            "-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4",
            "-o", str(work / "video.%(ext)s")]
    if cookies:
        base += ["--cookies", cookies]
    last = ""
    for extra in ([], ["--impersonate", "chrome"]):
        r = run(base + extra + [url])
        if r.returncode == 0 and (work / "video.mp4").exists():
            return
        last = ((r.stderr or "") + (r.stdout or ""))[-1800:]
    raise RuntimeError("download failed: " + last)


def probe(video):
    r = run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height,codec_type",
             "-show_entries", "format=duration", "-of", "json", str(video)])
    data = json.loads(r.stdout or "{}")
    dur = float(data.get("format", {}).get("duration", 0) or 0)
    w = h = 0
    has_audio = False
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not w:
            w, h = s.get("width", 0), s.get("height", 0)
        if s.get("codec_type") == "audio":
            has_audio = True
    return dur, w, h, has_audio


def transcribe(video, work):
    global _MODEL
    wav = work / "audio.wav"
    run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    from faster_whisper import WhisperModel
    if _MODEL is None:
        _MODEL = WhisperModel("small", device="cpu", compute_type="int8")
    # Decode the 16 kHz mono WAV ourselves: faster-whisper's own decoder (PyAV) can mismatch versions.
    import wave
    import numpy as np
    with wave.open(str(wav), "rb") as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    segs, info = _MODEL.transcribe(audio, vad_filter=True, beam_size=5)
    out = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs]
    return info.language, out


def scene_cuts(video):
    r = run(["ffmpeg", "-i", str(video), "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
             "-f", "null", "-"])
    return sorted({round(float(m), 2) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr or "")})


def pick_frame_times(cuts, dur):
    cand = sorted({0.3, *[c for c in cuts if c < dur - 0.3], max(dur - 0.6, 0.3)})
    kept = []
    for t in cand:
        if not kept or t - kept[-1] >= MIN_SPACING:
            kept.append(t)
    if len(kept) > MAX_FRAMES:
        step = len(kept) / MAX_FRAMES
        kept = [kept[int(i * step)] for i in range(MAX_FRAMES)]
    if len(kept) < MIN_FRAMES and dur > 0:
        kept = sorted({round(dur * (i + 0.5) / MIN_FRAMES, 2) for i in range(MIN_FRAMES)} | set(kept))
    return kept


def extract_frames(video, times, frames_dir):
    frames_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for t in times:
        p = frames_dir / f"f_{t:07.2f}.jpg"
        run(["ffmpeg", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1",
             "-vf", "scale=540:-2", "-q:v", "4", str(p)])
        if p.exists():
            paths.append((t, p))
    return paths


def ocr_frame(path):
    r = run(["tesseract", str(path), "stdout", "-l", "eng", "--psm", "6"])
    text = re.sub(r"\s+", " ", (r.stdout or "")).strip()
    return text if len(re.sub(r"[^A-Za-z0-9]", "", text)) >= 4 else ""


def contact_sheet(frames, out_path, cols=4):
    from PIL import Image, ImageDraw
    if not frames:
        return
    thumbs = []
    for t, p in frames:
        im = Image.open(p).convert("RGB")
        im.thumbnail((270, 480))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 62, 16], fill=(0, 0, 0))
        d.text((3, 2), f"{int(t // 60)}:{t % 60:04.1f}", fill=(255, 255, 255))
        thumbs.append(im)
    w = max(i.width for i in thumbs)
    h = max(i.height for i in thumbs)
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * h), (255, 255, 255))
    for n, im in enumerate(thumbs):
        sheet.paste(im, ((n % cols) * w, (n // cols) * h))
    sheet.save(out_path, quality=70)


def mmss(t):
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


def analyze_one(url, idx, out_root, cookies):
    out = out_root / f"{idx:02d}"
    work = Path("/tmp/va_work") / f"{idx:02d}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    out.mkdir(parents=True, exist_ok=True)

    download(url, work, cookies)
    video = work / "video.mp4"
    info = {}
    ij = work / "video.info.json"
    if ij.exists():
        info = json.loads(ij.read_text(encoding="utf-8"))
    dur, w, h, has_audio = probe(video)
    meta = {
        "url": url, "title": info.get("title"), "caption": info.get("description"),
        "uploader": info.get("uploader") or info.get("channel"), "view_count": info.get("view_count"),
        "like_count": info.get("like_count"), "comment_count": info.get("comment_count"),
        "upload_date": info.get("upload_date"), "duration_s": round(dur, 2),
        "width": w, "height": h, "has_audio": has_audio,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    lang, segs = ("", [])
    if has_audio:
        lang, segs = transcribe(video, work)
    full = " ".join(s["text"] for s in segs).strip()
    words = len(full.split())
    speech_time = sum(s["end"] - s["start"] for s in segs) or 1
    tr = {"language": lang, "segments": segs, "full_text": full, "word_count": words,
          "words_per_minute_total": round(words / (dur / 60), 1) if dur else 0,
          "words_per_minute_speaking": round(words / (speech_time / 60), 1)}
    (out / "transcript.json").write_text(json.dumps(tr, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "transcript.txt").write_text(
        "\n".join(f"[{mmss(s['start'])}] {s['text']}" for s in segs) + "\n", encoding="utf-8")

    cuts = scene_cuts(video)
    shots = {"scene_cuts_s": cuts, "shot_count": len(cuts) + 1,
             "avg_shot_length_s": round(dur / (len(cuts) + 1), 2) if dur else 0}
    (out / "shots.json").write_text(json.dumps(shots, indent=2), encoding="utf-8")

    frames = extract_frames(video, pick_frame_times(cuts, dur), out / "frames")
    contact_sheet(frames, out / "sheet.jpg")
    ocr = [{"t": t, "file": f"frames/{p.name}", "text": ocr_frame(p)} for t, p in frames]
    (out / "ocr.json").write_text(json.dumps(ocr, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [f"# {meta['title'] or url}", "",
             f"- URL: {url}", f"- Uploader: {meta['uploader']}",
             f"- Views / likes / comments: {meta['view_count']} / {meta['like_count']} / {meta['comment_count']}",
             f"- Duration: {meta['duration_s']} s, {w}x{h}, language: {lang or 'n/a'}",
             f"- Words: {words} ({tr['words_per_minute_speaking']} wpm while speaking)",
             f"- Shots: {shots['shot_count']} (average {shots['avg_shot_length_s']} s per shot)", "",
             "## Caption", "", (meta["caption"] or "(none)"), "", "## Transcript", "", full or "(no speech found)", "",
             "## On-screen text found", ""]
    seen = [o for o in ocr if o["text"]]
    lines += [f"- [{mmss(o['t'])}] {o['text']}" for o in seen] or ["(none)"]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    shutil.rmtree(work, ignore_errors=True)
    return {"url": url, "dir": f"{idx:02d}", "ok": True, "title": meta["title"],
            "duration_s": meta["duration_s"], "words": words, "shots": shots["shot_count"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", default="analysis")
    ap.add_argument("--cookies", default="")
    a = ap.parse_args()

    label = re.sub(r"[^A-Za-z0-9_.-]", "-", a.label).strip("-") or "run"
    root = Path(a.out) / label
    root.mkdir(parents=True, exist_ok=True)
    urls = parse_urls(a.urls)
    if not urls:
        print("No valid URLs given.", file=sys.stderr)
        (root / "index.json").write_text(json.dumps([{"ok": False, "error": "no valid urls"}]))
        return
    index = []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}", file=sys.stderr)
        try:
            index.append(analyze_one(url, i, root, a.cookies or None))
        except Exception as e:  # one bad video must not stop the rest
            traceback.print_exc()
            index.append({"url": url, "dir": f"{i:02d}", "ok": False, "error": str(e)[:1800]})
        (root / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    ok = sum(1 for x in index if x.get("ok"))
    print(f"Done: {ok}/{len(urls)} videos analyzed.", file=sys.stderr)


if __name__ == "__main__":
    main()
