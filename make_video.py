# ساخت یک ویدیوی شورت از صف Supabase و آپلود خصوصی در یوتوب
import asyncio
import os
import random
import subprocess
import sys
import tempfile
import textwrap
import time

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
PIXABAY_KEY = os.environ["PIXABAY_API_KEY"]

VOICE = "en-US-AndrewNeural"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FALLBACK_TERMS = ["nature landscape", "abstract background", "city lights"]
MAX_SECONDS = 170  # حداکثر طول شورت


# ---------- Supabase ----------
def supa(method, path, **kwargs):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    headers.update(kwargs.pop("headers", {}))
    r = requests.request(
        method, f"{SUPABASE_URL}/rest/v1/{path}", headers=headers, timeout=60, **kwargs
    )
    r.raise_for_status()
    return r.json() if r.text else None


def claim_next():
    # اولین ویدیوی در صف را برمی‌دارد و وضعیتش را building می‌کند
    rows = supa("GET", "yt_videos?status=eq.queued&order=id.asc&limit=1")
    if not rows:
        return None
    row = rows[0]
    claimed = supa(
        "PATCH",
        f"yt_videos?id=eq.{row['id']}&status=eq.queued",
        json={"status": "building"},
        headers={"Prefer": "return=representation"},
    )
    return row if claimed else None


# ---------- صدا ----------
async def _edge_tts(text, path):
    import edge_tts

    await edge_tts.Communicate(text, VOICE, rate="+4%").save(path)


def make_audio(text, path):
    for attempt in range(3):
        try:
            asyncio.run(_edge_tts(text, path))
            if os.path.getsize(path) > 1000:
                return
        except Exception as e:
            print("edge-tts failed:", e)
            time.sleep(2)
    # راه جایگزین اگر edge-tts کار نکرد
    from gtts import gTTS

    gTTS(text, lang="en", tld="com").save(path)


def duration(path):
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", path,
        ]
    )
    return float(out.decode().strip())


# ---------- Pixabay ----------
def pixabay_clip(term, used):
    r = requests.get(
        "https://pixabay.com/api/videos/",
        params={"key": PIXABAY_KEY, "q": term[:100], "per_page": 20, "safesearch": "true"},
        timeout=30,
    )
    r.raise_for_status()
    hits = r.json().get("hits", [])
    random.shuffle(hits)
    for h in hits:
        if h["id"] in used:
            continue
        videos = h.get("videos", {})
        for size in ("large", "medium"):
            v = videos.get(size) or {}
            # از فایل‌های خیلی حجیم صرف‌نظر می‌کنیم
            if v.get("url") and v.get("size", 0) < 60_000_000:
                used.add(h["id"])
                return v["url"]
    return None


def download(url, path):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)


def get_clip(term, used, path):
    for t in [term] + FALLBACK_TERMS:
        link = pixabay_clip(t, used)
        if link:
            download(link, path)
            return
    raise RuntimeError(f"no clip found for: {term}")


# ---------- ساخت صحنه‌ها ----------
def build_scene(i, sentence, term, work, used):
    audio = os.path.join(work, f"s{i}.mp3")
    clip = os.path.join(work, f"c{i}.mp4")
    out = os.path.join(work, f"scene{i}.mp4")

    make_audio(sentence, audio)
    dur = duration(audio) + 0.3
    get_clip(term, used, clip)

    # هر خط زیرنویس یک drawtext جداست تا کاراکتر خط جدید به شکل مربع نمایش داده نشود
    lines = textwrap.wrap(sentence, width=24)
    filters = ["scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30"]
    y0 = int(1180 - len(lines) * 52)
    for j, line in enumerate(lines):
        line_file = os.path.join(work, f"t{i}_{j}.txt")
        with open(line_file, "w", encoding="utf-8") as f:
            f.write(line)
        filters.append(
            f"drawtext=fontfile={FONT}:textfile={line_file}:expansion=none:"
            "fontcolor=white:fontsize=62:borderw=3:bordercolor=black:"
            "box=1:boxcolor=black@0.45:boxborderw=18:"
            f"x=(w-text_w)/2:y={y0 + j * 104}"
        )
    vf = ",".join(filters)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-stream_loop", "-1", "-i", clip, "-i", audio,
            "-t", f"{dur:.2f}", "-vf", vf, "-af", "apad",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            out,
        ],
        check=True,
    )
    return out, dur


def build_video(row, work):
    used = set()
    scenes, total = [], 0.0
    for i, (sentence, term) in enumerate(zip(row["narration"], row["search_terms"])):
        path, dur = build_scene(i, sentence, term, work, used)
        scenes.append(path)
        total += dur
    if total > MAX_SECONDS:
        raise RuntimeError(f"video too long: {total:.0f}s")

    lst = os.path.join(work, "list.txt")
    with open(lst, "w") as f:
        for p in scenes:
            f.write(f"file '{p}'\n")
    final = os.path.join(work, "final.mp4")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
            "-i", lst, "-c", "copy", "-movflags", "+faststart", final,
        ],
        check=True,
    )
    print(f"video ready: {total:.0f}s")
    return final


# ---------- آپلود در یوتوب (خصوصی) ----------
def upload(row, path):
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)

    title = row["title"].strip()
    if len(title) + 8 <= 100:
        title += " #Shorts"
    body = {
        "snippet": {
            "title": title,
            "description": row["description"].strip() + "\n\nStock footage: Pixabay\n#Shorts",
            "tags": row.get("tags") or [],
            "categoryId": "27",  # Education
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(path, mimetype="video/mp4", chunksize=-1, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


def main():
    row = claim_next()
    if not row:
        print("queue is empty")
        return
    print("working on:", row["title"])
    try:
        with tempfile.TemporaryDirectory() as work:
            final = build_video(row, work)
            video_id = upload(row, final)
        supa(
            "PATCH",
            f"yt_videos?id=eq.{row['id']}",
            json={"status": "uploaded", "youtube_id": video_id},
        )
        print("uploaded (private):", video_id)
    except Exception as e:
        supa(
            "PATCH",
            f"yt_videos?id=eq.{row['id']}",
            json={"status": "failed", "error": str(e)[:500]},
        )
        print("failed:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
