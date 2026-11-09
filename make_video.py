# ساخت یک ویدیوی شورت از صف Supabase و آپلود خصوصی در یوتوب
# نسخه ۳: ترتیب تصویر هر صحنه: ۱) کلیپ Pixabay ۲) تصویر AI (Cloudflare FLUX) با زوم ۳) عکس Pixabay ۴) کلیپ عمومی
# عکس/ویدیوی خود مهدی از باکت media (اختیاری) همچنان اولویت اول دارد
import asyncio
import base64
import os
import random
import re
import subprocess
import sys
import tempfile
import textwrap
import time

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
PIXABAY_KEY = os.environ["PIXABAY_API_KEY"]
# اختیاری: اگر نباشند، تصویر AI رد می‌شود و بقیه مراحل کار می‌کنند
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()

VOICE = "en-US-AndrewNeural"
FONT_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-ExtraBold.ttf"
FONT = FONT_FALLBACK  # در main() اگر دانلود موفق بود عوض می‌شود
FALLBACK_TERMS = ["nature landscape", "abstract background", "city lights"]
MAX_SECONDS = 170  # حداکثر طول شورت
BUCKET = "media"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}

AI_MODEL = "@cf/black-forest-labs/flux-1-schnell"
MAX_AI_PER_VIDEO = 4  # سقف تصویر AI در هر ویدیو برای حفظ سهمیه‌ی رایگان Workers AI
AI_STYLE = (
    "cinematic documentary still, realistic, dramatic lighting, vertical composition, "
    "no text, no captions, no logos, no recognizable real person's face"
)

# آمار این اجرا (برای نوشتن منبع‌ها در توضیحات)
STATS = {"stock": 0, "ai": 0}


# ---------- فونت ----------
def setup_font(work):
    global FONT
    path = os.path.join(work, "Poppins-ExtraBold.ttf")
    try:
        r = requests.get(FONT_URL, timeout=60)
        r.raise_for_status()
        if len(r.content) > 50_000:
            with open(path, "wb") as f:
                f.write(r.content)
            FONT = path
            print("font: Poppins ExtraBold")
            return
    except Exception as e:
        print("font download failed, using DejaVu:", e)
    FONT = FONT_FALLBACK


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


# ---------- فایل‌های مهدی (Supabase Storage) ----------
def storage_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def list_media():
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/list/{BUCKET}",
        headers={**storage_headers(), "Content-Type": "application/json"},
        json={"prefix": "", "limit": 1000, "offset": 0},
        timeout=60,
    )
    r.raise_for_status()
    return [o["name"] for o in r.json() if o.get("name") and o.get("id")]


def find_assets(video_id):
    # اسم فایل: v3 یا v3-1 یا v3-2 ... → شماره‌ی slot (بدون شماره = slot 1)
    found = {}
    pat = re.compile(rf"^v{video_id}(?:-(\d+))?$", re.IGNORECASE)
    try:
        names = list_media()
    except Exception as e:
        print("media list failed:", e)
        return found
    for name in names:
        stem, ext = os.path.splitext(name)
        m = pat.match(stem)
        if not m or ext.lower() not in IMAGE_EXT | VIDEO_EXT:
            continue
        slot = int(m.group(1)) if m.group(1) else 1
        found[slot] = name
    return found


def download_asset(name, work):
    path = os.path.join(work, "asset_" + name)
    with requests.get(
        f"{SUPABASE_URL}/storage/v1/object/authenticated/{BUCKET}/{name}",
        headers=storage_headers(), stream=True, timeout=120,
    ) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)
    return path


def delete_assets(names):
    for name in names:
        try:
            r = requests.delete(
                f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{name}",
                headers=storage_headers(), timeout=60,
            )
            print("deleted media:" if r.ok else "delete failed:", name)
        except Exception as e:
            print("delete failed:", name, e)


def sentence_slots(row):
    # sentence index (0-based) → slot number
    mapping = {}
    slots = ((row.get("visuals") or {}).get("slots")) or []
    for s in slots:
        for k in range(int(s["from"]), int(s["to"]) + 1):
            mapping[k - 1] = int(s["n"])
    return mapping


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
def download(url, path):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)


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
        key = ("v", h["id"])
        if key in used:
            continue
        videos = h.get("videos", {})
        for size in ("large", "medium"):
            v = videos.get(size) or {}
            # از فایل‌های خیلی حجیم صرف‌نظر می‌کنیم
            if v.get("url") and v.get("size", 0) < 60_000_000:
                used.add(key)
                return v["url"]
    return None


def pixabay_photo(term, used):
    r = requests.get(
        "https://pixabay.com/api/",
        params={
            "key": PIXABAY_KEY, "q": term[:100], "image_type": "photo",
            "orientation": "vertical", "per_page": 20, "safesearch": "true",
        },
        timeout=30,
    )
    r.raise_for_status()
    hits = r.json().get("hits", [])
    random.shuffle(hits)
    for h in hits:
        key = ("p", h["id"])
        if key in used:
            continue
        link = h.get("largeImageURL") or h.get("webformatURL")
        if link:
            used.add(key)
            return link
    return None


# ---------- تصویر AI (Cloudflare Workers AI، FLUX.1 schnell) ----------
def ai_image(term, sentence, path):
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        return False
    if STATS["ai"] >= MAX_AI_PER_VIDEO:
        return False
    prompt = f"{term}. Scene: {sentence[:200]}. {AI_STYLE}"
    try:
        r = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{AI_MODEL}",
            headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
            json={"prompt": prompt[:2000], "steps": 4},
            timeout=90,
        )
        r.raise_for_status()
        data = base64.b64decode(r.json()["result"]["image"])
        if len(data) < 5000:
            print("ai image too small")
            return False
        with open(path, "wb") as f:
            f.write(data)
        STATS["ai"] += 1
        print("ai image ok:", term[:50])
        return True
    except Exception as e:
        print("ai image failed:", str(e)[:200])
        return False


def get_visual(i, term, sentence, used, work):
    # برمی‌گرداند: مسیر فایل (ویدیو یا عکس)
    clip_path = os.path.join(work, f"c{i}.mp4")
    img_path = os.path.join(work, f"i{i}.jpg")

    # ۱) کلیپ Pixabay با عبارت جستجوی خود صحنه
    try:
        link = pixabay_clip(term, used)
        if link:
            download(link, clip_path)
            STATS["stock"] += 1
            return clip_path
    except Exception as e:
        print("pixabay clip failed:", str(e)[:150])

    # ۲) تصویر AI
    if ai_image(term, sentence, img_path):
        return img_path

    # ۳) عکس Pixabay
    try:
        link = pixabay_photo(term, used)
        if link:
            download(link, img_path)
            STATS["stock"] += 1
            return img_path
    except Exception as e:
        print("pixabay photo failed:", str(e)[:150])

    # ۴) آخرین راه: کلیپ عمومی
    for t in FALLBACK_TERMS:
        try:
            link = pixabay_clip(t, used)
            if link:
                download(link, clip_path)
                STATS["stock"] += 1
                return clip_path
        except Exception as e:
            print("fallback clip failed:", str(e)[:150])
    raise RuntimeError(f"no visual found for: {term}")


# ---------- زیرنویس ----------
def subtitle_filters(sentence, i, work):
    # هر خط زیرنویس یک drawtext جداست تا کاراکتر خط جدید به شکل مربع نمایش داده نشود
    lines = textwrap.wrap(sentence, width=22)
    out = []
    y0 = int(1200 - len(lines) * 55)
    for j, line in enumerate(lines):
        line_file = os.path.join(work, f"t{i}_{j}.txt")
        with open(line_file, "w", encoding="utf-8") as f:
            f.write(line)
        out.append(
            f"drawtext=fontfile={FONT}:textfile={line_file}:expansion=none:"
            "fontcolor=white:fontsize=66:borderw=5:bordercolor=black@0.95:"
            "shadowcolor=black@0.6:shadowx=0:shadowy=5:"
            f"x=(w-text_w)/2:y={y0 + j * 110}"
        )
    return out


# ---------- ساخت صحنه‌ها ----------
def build_scene(i, sentence, term, work, used, asset=None):
    audio = os.path.join(work, f"s{i}.mp3")
    out = os.path.join(work, f"scene{i}.mp4")

    make_audio(sentence, audio)
    dur = duration(audio) + 0.3
    subs = subtitle_filters(sentence, i, work)
    if not asset:
        asset = get_visual(i, term, sentence, used, work)
    ext = os.path.splitext(asset)[1].lower()
    frames = int(dur * 30) + 2

    if ext in IMAGE_EXT:
        # عکس: پس‌زمینه‌ی محو + خود عکس کامل وسط + زوم آرام (جهت زوم یکی در میان عوض می‌شود)
        if i % 2 == 0:
            zoom = "min(1+0.0007*on,1.18)"
        else:
            zoom = "max(1.18-0.0007*on,1.0)"
        fc = (
            "[0:v]split[a][b];"
            "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=40:6,eq=brightness=-0.08[bg];"
            "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
            f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1080x1920:fps=30,"
            + ",".join(subs)
            + "[v]"
        )
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-i", asset, "-i", audio,
            "-t", f"{dur:.2f}", "-filter_complex", fc, "-af", "apad",
            "-map", "[v]", "-map", "1:a",
        ]
    else:
        filters = ["scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30"] + subs
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-stream_loop", "-1", "-i", asset, "-i", audio,
            "-t", f"{dur:.2f}", "-vf", ",".join(filters), "-af", "apad",
            "-map", "0:v", "-map", "1:a",
        ]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
        out,
    ]
    subprocess.run(cmd, check=True)
    return out, dur


def build_video(row, work):
    used = set()
    scenes, total = [], 0.0
    slot_of = sentence_slots(row)
    assets = find_assets(row["id"]) if slot_of else {}
    local = {}  # slot → مسیر محلی
    used_names = set()
    for i, (sentence, term) in enumerate(zip(row["narration"], row["search_terms"])):
        slot = slot_of.get(i)
        asset_path = None
        if slot in assets:
            if slot not in local:
                local[slot] = download_asset(assets[slot], work)
            asset_path = local[slot]
            used_names.add(assets[slot])
        path, dur = build_scene(i, sentence, term, work, used, asset_path)
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
    print(f"video ready: {total:.0f}s; own media: {sorted(used_names)}; stats: {STATS}")
    return final, sorted(used_names)


# ---------- آپلود در یوتوب (خصوصی) ----------
def upload(row, path, own_media):
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
    credits = []
    if own_media:
        credits.append("Images: Wikimedia Commons and other freely licensed sources")
    if STATS["stock"]:
        credits.append("Stock footage and photos: Pixabay")
    if STATS["ai"]:
        credits.append(
            "Some images are AI-generated illustrations (Cloudflare Workers AI, FLUX.1 schnell) "
            "and are illustrative, not real photographs."
        )
    desc = row["description"].strip() + "\n\n" + "\n".join(credits) + "\n#Shorts"
    body = {
        "snippet": {
            "title": title,
            "description": desc,
            "tags": row.get("tags") or [],
            "categoryId": "27",  # Education
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    # اعلام محتوای مصنوعی وقتی تصویر AI استفاده شده؛ اگر API این فیلد را نپذیرفت بدونش دوباره تلاش می‌کنیم
    if STATS["ai"]:
        body["status"]["containsSyntheticMedia"] = True

    def do_upload(b):
        media = MediaFileUpload(path, mimetype="video/mp4", chunksize=-1, resumable=True)
        request = yt.videos().insert(part="snippet,status", body=b, media_body=media)
        response = None
        while response is None:
            _, response = request.next_chunk()
        return response["id"]

    try:
        return do_upload(body)
    except HttpError as e:
        if "containsSyntheticMedia" in body["status"] and getattr(e.resp, "status", 0) == 400:
            print("containsSyntheticMedia rejected, retrying without it:", str(e)[:200])
            body["status"].pop("containsSyntheticMedia")
            return do_upload(body)
        raise


def main():
    row = claim_next()
    if not row:
        print("queue is empty")
        return
    print("working on:", row["title"])
    try:
        with tempfile.TemporaryDirectory() as work:
            setup_font(work)
            final, own_media = build_video(row, work)
            video_id = upload(row, final, own_media)
        supa(
            "PATCH",
            f"yt_videos?id=eq.{row['id']}",
            json={"status": "uploaded", "youtube_id": video_id},
        )
        print("uploaded (private):", video_id)
        # حذف خودکار فقط فایل‌هایی که واقعاً در این ویدیو استفاده شدند
        delete_assets(own_media)
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
