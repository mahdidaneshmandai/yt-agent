# ساخت یک ویدیوی شورت از صف Supabase و آپلود در یوتوب
# نسخه ۵:
#  - کپشن کلمه‌به‌کلمه با زمان‌بندی دقیق خود edge-tts (بدون نیاز به Whisper)
#  - نوار پیشرفت بالای ویدیو (قطعه‌به‌قطعه: 1/N ... N/N)
#  - برچسب کوتاه هر صحنه (labels) و مهر بزرگ روی نکته‌ی اصلی (stamps)
#  - نام سری به‌عنوان برند ثابت گوشه‌ی بالا
#  - هر قابلیت جدید اگر خراب شود، به رفتار قبلی برمی‌گردد (ویدیو هیچ‌وقت به‌خاطر ظاهر خراب نمی‌شود)
#  - هر صحنه نوع دارد: footage (کلیپ Pixabay اول) یا illustration (تصویر AI اول)
#  - ردیف‌های با priority بالاتر زودتر ساخته می‌شوند
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

# وضعیت انتشار: فقط وقتی دستی از تب Actions «public» انتخاب شود عمومی می‌شود؛ اجرای خودکار (cron) همیشه private است
PRIVACY = os.environ.get("PRIVACY", "private").strip().lower()
if PRIVACY not in ("private", "public"):
    PRIVACY = "private"

VOICE = "en-US-AndrewNeural"
FONT_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-ExtraBold.ttf"
FONT = FONT_FALLBACK  # در main() اگر دانلود موفق بود عوض می‌شود
FALLBACK_TERMS = ["nature landscape", "abstract background", "city lights"]
MAX_SECONDS = 175  # حداکثر طول شورت (یوتوب تا ۳ دقیقه قبول می‌کند)
BUCKET = "media"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}

AI_MODEL = "@cf/black-forest-labs/flux-1-schnell"
AI_STEPS = 8  # حداکثر مرحله‌ی schnell؛ هر تصویر حدود ۱۰۰ neuron
MAX_AI_PER_VIDEO = 20  # سقف تصویر AI در هر ویدیو (حدود ۲۰۰۰ neuron)
AI_STYLE = (
    "photorealistic documentary still, vertical composition, "
    "no text, no captions, no logos, no recognizable real person's face"
)

# ---------- تنظیمات ظاهر (رنگ‌ها و اندازه‌ها) ----------
ACCENT = "0xF2994A"  # نارنجی برند
CAPTION_FONTSIZE = 84
CAPTION_MAX_CHARS = 17  # حداکثر حرف در هر تکه‌ی کپشن
CAPTION_MAX_WORDS = 3  # حداکثر کلمه در هر تکه‌ی کپشن
BAR_Y, BAR_H = 70, 12  # جای نوار پیشرفت
BRAND_Y = 108
LABEL_Y = 190
STAMP_Y = 470
CAPTION_Y = 1130  # بالاتر از ناحیه‌ی دکمه‌های یوتوب
STAMP_COLORS = {
    "TRUE": "0x2E9E5B", "FREE": "0x2E9E5B", "REAL": "0x2E9E5B", "CONFIRMED": "0x2E9E5B",
    "FALSE": "0xD64545", "BANNED": "0xD64545", "MYTH": "0xD64545", "FAKE": "0xD64545",
}

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
    # ویدیوی با بیشترین priority (و بعد قدیمی‌ترین) را برمی‌دارد و وضعیتش را building می‌کند
    rows = supa("GET", "yt_videos?status=eq.queued&order=priority.desc,id.asc&limit=1")
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
    # صدا را می‌نویسد و زمان شروع/پایان هر کلمه را هم برمی‌گرداند
    # edge-tts نسخه ۷ به‌طور پیش‌فرض فقط زمان جمله می‌دهد؛ boundary را صریح می‌دهیم
    import edge_tts

    try:
        com = edge_tts.Communicate(text, VOICE, rate="+4%", boundary="WordBoundary")
    except TypeError:
        com = edge_tts.Communicate(text, VOICE, rate="+4%")
    words = []
    with open(path, "wb") as f:
        async for chunk in com.stream():
            kind = chunk.get("type")
            if kind == "audio":
                f.write(chunk["data"])
            elif kind == "WordBoundary":
                start = chunk["offset"] / 1e7  # واحد ۱۰۰ نانوثانیه
                end = start + chunk["duration"] / 1e7
                words.append((chunk["text"], start, end))
    return words


def make_audio(text, path):
    # برمی‌گرداند: فهرست (کلمه، شروع، پایان)؛ اگر زمان کلمه‌ها نبود، فهرست خالی
    for attempt in range(3):
        try:
            words = asyncio.run(_edge_tts(text, path))
            if os.path.getsize(path) > 1000:
                return words
        except Exception as e:
            print("edge-tts failed:", e)
            time.sleep(2)
    # راه جایگزین اگر edge-tts کار نکرد
    from gtts import gTTS

    gTTS(text, lang="en", tld="com").save(path)
    return []


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
def ai_image(prompt_core, path):
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        return False
    if STATS["ai"] >= MAX_AI_PER_VIDEO:
        return False
    prompt = f"{prompt_core}. {AI_STYLE}"
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{AI_MODEL}",
                headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
                json={"prompt": prompt[:2000], "steps": AI_STEPS},
                timeout=120,
            )
            if r.status_code in (429, 502, 503, 504):
                # ظرفیت پر یا خطای موقت؛ کمی صبر و دوباره
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            data = base64.b64decode(r.json()["result"]["image"])
            if len(data) < 5000:
                print("ai image too small")
                return False
            with open(path, "wb") as f:
                f.write(data)
            STATS["ai"] += 1
            print("ai image ok:", prompt_core[:60])
            return True
        except Exception as e:
            print("ai image failed:", str(e)[:200])
            if attempt < 2:
                time.sleep(3)
    return False


def get_visual(i, term, sentence, used, work, image_prompt=None, scene_type="footage"):
    # برمی‌گرداند: مسیر فایل (ویدیو یا عکس)
    clip_path = os.path.join(work, f"c{i}.mp4")
    img_path = os.path.join(work, f"i{i}.jpg")

    def try_clip():
        try:
            link = pixabay_clip(term, used)
            if link:
                download(link, clip_path)
                STATS["stock"] += 1
                return clip_path
        except Exception as e:
            print("pixabay clip failed:", str(e)[:150])
        return None

    def try_ai():
        core = image_prompt or f"{term}. Scene: {sentence[:200]}"
        return img_path if ai_image(core, img_path) else None

    def try_photo():
        try:
            link = pixabay_photo(term, used)
            if link:
                download(link, img_path)
                STATS["stock"] += 1
                return img_path
        except Exception as e:
            print("pixabay photo failed:", str(e)[:150])
        return None

    # صحنه‌ی illustration: اول تصویر AI؛ صحنه‌ی footage: اول کلیپ
    order = [try_ai, try_clip, try_photo] if scene_type == "illustration" else [try_clip, try_ai, try_photo]
    for step in order:
        got = step()
        if got:
            return got

    # آخرین راه: کلیپ عمومی
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


# ---------- لایه‌های ظاهری (همه با drawtext/drawbox؛ هر متن در فایل جدا) ----------
def _text_file(work, name, text):
    path = os.path.join(work, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def caption_chunks(words):
    # کلمه‌ها را به تکه‌های ۱ تا ۳ کلمه‌ای تقسیم می‌کند؛ هر تکه تا شروع تکه‌ی بعد روی صفحه می‌ماند
    chunks, cur = [], []
    for w in words:
        text = w[0].strip()
        if not text:
            continue
        cur_text = " ".join(x[0] for x in cur)
        too_long = len(cur_text) + 1 + len(text) > CAPTION_MAX_CHARS
        if cur and (len(cur) >= CAPTION_MAX_WORDS or too_long):
            chunks.append(cur)
            cur = []
        cur.append((text, w[1], w[2]))
        if text[-1] in ".,?!;:":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    out = []
    for k, ch in enumerate(chunks):
        start = ch[0][1]
        end = chunks[k + 1][0][1] if k + 1 < len(chunks) else ch[-1][2] + 0.25
        out.append((" ".join(x[0] for x in ch), start, end))
    return out


def word_caption_filters(words, i, work):
    out = []
    for k, (text, start, end) in enumerate(caption_chunks(words)):
        path = _text_file(work, f"w{i}_{k}.txt", text)
        out.append(
            f"drawtext=fontfile={FONT}:textfile={path}:expansion=none:"
            f"fontcolor=white:fontsize={CAPTION_FONTSIZE}:borderw=6:bordercolor=black@0.95:"
            "shadowcolor=black@0.6:shadowx=0:shadowy=5:"
            f"x=(w-text_w)/2:y={CAPTION_Y}:enable='between(t,{start:.3f},{end:.3f})'"
        )
    return out


def subtitle_filters(sentence, i, work):
    # راه جایگزین (وقتی زمان کلمه‌ها نیست): هر خط زیرنویس یک drawtext جداست
    lines = textwrap.wrap(sentence, width=22)
    out = []
    y0 = int(1200 - len(lines) * 55)
    for j, line in enumerate(lines):
        line_file = _text_file(work, f"t{i}_{j}.txt", line)
        out.append(
            f"drawtext=fontfile={FONT}:textfile={line_file}:expansion=none:"
            "fontcolor=white:fontsize=66:borderw=5:bordercolor=black@0.95:"
            "shadowcolor=black@0.6:shadowx=0:shadowy=5:"
            f"x=(w-text_w)/2:y={y0 + j * 110}"
        )
    return out


def progress_filters(i, n):
    # نوار پیشرفت: n قطعه؛ قطعه‌های تا صحنه‌ی فعلی نارنجی، بقیه کمرنگ
    if n < 1:
        return []
    margin, gap = 60, 6
    seg = max(8, (1080 - 2 * margin - gap * (n - 1)) // n)
    out = []
    for k in range(n):
        color = ACCENT if k <= i else "white@0.30"
        out.append(
            f"drawbox=x={margin + k * (seg + gap)}:y={BAR_Y}:w={seg}:h={BAR_H}:color={color}:t=fill"
        )
    return out


def brand_filters(series, i, work):
    if not series:
        return []
    path = _text_file(work, f"b{i}.txt", series.upper()[:28])
    return [
        f"drawtext=fontfile={FONT}:textfile={path}:expansion=none:fontcolor={ACCENT}:"
        f"fontsize=34:borderw=3:bordercolor=black@0.9:x=60:y={BRAND_Y}"
    ]


def label_filters(label, i, work):
    if not label:
        return []
    path = _text_file(work, f"l{i}.txt", label.upper()[:32])
    return [
        f"drawtext=fontfile={FONT}:textfile={path}:expansion=none:fontcolor=white:fontsize=44:"
        "box=1:boxcolor=black@0.55:boxborderw=18:"
        f"x=(w-text_w)/2:y={LABEL_Y}"
    ]


def stamp_filters(stamp, i, work, dur):
    # مهر بزرگ روی نکته‌ی اصلی؛ بعد از ۰.۳ ثانیه تا آخر صحنه می‌ماند
    if not stamp:
        return []
    text = stamp.upper()[:14]
    color = STAMP_COLORS.get(text, ACCENT)
    path = _text_file(work, f"s{i}_stamp.txt", text)
    return [
        f"drawtext=fontfile={FONT}:textfile={path}:expansion=none:fontcolor=white:fontsize=130:"
        f"box=1:boxcolor={color}@0.92:boxborderw=28:"
        f"x=(w-text_w)/2:y={STAMP_Y}:enable='between(t,0.3,{dur:.2f})'"
    ]


def overlay_filters(sentence, words, i, n, series, label, stamp, work, dur):
    # همه‌ی لایه‌های روی تصویر به ترتیب: نوار، برند، برچسب، مهر، کپشن
    layers = []
    try:
        layers += progress_filters(i, n)
        layers += brand_filters(series, i, work)
        layers += label_filters(label, i, work)
        layers += stamp_filters(stamp, i, work, dur)
    except Exception as e:
        print("overlay layers failed, skipping them:", str(e)[:150])
        layers = []
    caps = []
    if words:
        try:
            caps = word_caption_filters(words, i, work)
        except Exception as e:
            print("word captions failed, using sentence captions:", str(e)[:150])
    if not caps:
        caps = subtitle_filters(sentence, i, work)
    return layers + caps


# ---------- ساخت صحنه‌ها ----------
def run_scene_ffmpeg(asset, audio, dur, overlays, i, out):
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
            + ",".join(overlays)
            + "[v]"
        )
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-i", asset, "-i", audio,
            "-t", f"{dur:.2f}", "-filter_complex", fc, "-af", "apad",
            "-map", "[v]", "-map", "1:a",
        ]
    else:
        filters = ["scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30"] + overlays
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


def build_scene(i, sentence, term, work, used, asset=None, image_prompt=None,
                scene_type="footage", n=0, series="", label="", stamp=""):
    audio = os.path.join(work, f"s{i}.mp3")
    out = os.path.join(work, f"scene{i}.mp4")

    words = make_audio(sentence, audio)
    dur = duration(audio) + 0.3
    if not asset:
        asset = get_visual(i, term, sentence, used, work, image_prompt, scene_type)

    overlays = overlay_filters(sentence, words, i, n, series, label, stamp, work, dur)
    try:
        run_scene_ffmpeg(asset, audio, dur, overlays, i, out)
    except subprocess.CalledProcessError:
        # اگر لایه‌های جدید خطا دادند، با زیرنویس ساده‌ی قبلی دوباره می‌سازیم
        print(f"scene {i}: overlay render failed, retrying with plain subtitles")
        run_scene_ffmpeg(asset, audio, dur, subtitle_filters(sentence, i, work), i, out)
    return out, dur


def _at(arr, i):
    # عنصر i ام فهرست یا رشته‌ی خالی؛ ‎null‎ و کوتاه‌بودن فهرست را تحمل می‌کند
    if arr and i < len(arr) and arr[i]:
        return str(arr[i]).strip()
    return ""


def build_video(row, work):
    used = set()
    scenes, total = [], 0.0
    slot_of = sentence_slots(row)
    assets = find_assets(row["id"]) if slot_of else {}
    local = {}  # slot → مسیر محلی
    used_names = set()
    narration = row["narration"]
    terms = row["search_terms"]
    prompts = row.get("image_prompts") or []
    types = row.get("scene_types") or []
    labels = row.get("labels") or []
    stamps = row.get("stamps") or []
    series = (row.get("series") or "").strip()
    n = len(narration)
    for i, (sentence, term) in enumerate(zip(narration, terms)):
        slot = slot_of.get(i)
        asset_path = None
        if slot in assets:
            if slot not in local:
                local[slot] = download_asset(assets[slot], work)
            asset_path = local[slot]
            used_names.add(assets[slot])
        prompt = prompts[i] if i < len(prompts) else None
        stype = types[i] if i < len(types) and types[i] in ("footage", "illustration") else "footage"
        path, dur = build_scene(
            i, sentence, term, work, used, asset_path, prompt, stype,
            n=n, series=series, label=_at(labels, i), stamp=_at(stamps, i),
        )
        scenes.append(path)
        total += dur
        print(f"scene {i + 1}/{n} done ({stype}), total {total:.0f}s")
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


# ---------- آپلود در یوتوب ----------
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
            "privacyStatus": PRIVACY,
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
        print(f"uploaded ({PRIVACY}):", video_id)
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
