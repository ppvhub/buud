#!/usr/bin/env python3
"""Resumable WordPress video importer. Processes one batch per invocation."""
import argparse, base64, html, json, os, re, sys, time, random, subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = os.getenv("WORDPRESS_API", "https://baddiehub.com/wp-json/wp/v2/posts")
DATA = Path("assets/data/videos.json")
THUMBS = Path("assets/thumb")
STATE = Path("assets/data/.video_import_state.json")
USER_AGENT = "Mozilla/5.0 (compatible; VideoCatalogSync/3.0)"
REQUEST_TIMEOUT = 45
MAX_RETRIES = 5
MAX_THUMB_BYTES = 8 * 1024 * 1024
PER_PAGE = 100

def log(message): print(message, flush=True)

def fetch(url, timeout=REQUEST_TIMEOUT, headers=None):
    last = None
    for attempt in range(MAX_RETRIES + 1):
        req_headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json,*/*", "Connection": "close"}
        if headers: req_headers.update(headers)
        try:
            with urlopen(Request(url, headers=req_headers), timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except HTTPError as e:
            last = e
            if e.code not in (408, 425, 429, 500, 502, 503, 504): raise
        except (URLError, TimeoutError, OSError) as e: last = e
        if attempt < MAX_RETRIES:
            delay = min(2 ** attempt * 2, 30)
            log(f"Request retry {attempt+1}/{MAX_RETRIES}: {url} ({last}); wait {delay}s")
            time.sleep(delay)
    raise RuntimeError(f"Request failed after retries: {url}: {last}")

def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

def decode_url(video):
    try:
        raw = base64.b64decode(video["encrypted"]).decode("latin1")[::-1]
        key = video["iv"]
        return "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(raw)) if key else ""
    except Exception: return ""

def direct_mp4(url):
    try:
        p = urlparse(html.unescape(url.strip()))
        return p.scheme == "https" and bool(p.netloc) and p.path.lower().endswith(".mp4")
    except Exception: return False

def meta(content, key):
    pattern = r"<meta\b(?=[^>]*\b(?:itemprop|property|name)\s*=\s*[\"']" + re.escape(key) + r"[\"'])[^>]*>"
    for tag in re.findall(pattern, content, flags=re.I):
        m = re.search(r"""\bcontent\s*=\s*["']([^"']+)["']""", tag, flags=re.I)
        if m: return html.unescape(m.group(1)).strip()
    m = re.search(r"""["']""" + re.escape(key) + r"""["']\s*:\s*["']([^"']+)["']""", content, flags=re.I)
    return html.unescape(m.group(1)).strip() if m else ""

def parse_duration(value):
    value = (value or "").strip()
    if not value: return ""
    # ISO 8601 durations, e.g. PT12M34S / PT1H2M
    m = re.fullmatch(r"P(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)", value, re.I)
    if m:
        h, mi, sec = m.groups()
        total = int(h or 0)*3600 + int(mi or 0)*60 + int(float(sec or 0))
        return f"{total//3600:02d}:{(total%3600)//60:02d}:{total%60:02d}" if total >= 3600 else f"{total//60:02d}:{total%60:02d}"
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", value): return value
    if value.isdigit():
        total = int(value)
        return f"{total//3600:02d}:{(total%3600)//60:02d}:{total%60:02d}" if total >= 3600 else f"{total//60:02d}:{total%60:02d}"
    return ""

def find_video_url(content):
    value = meta(content, "contentUrl")
    if direct_mp4(value): return value
    for value in re.findall(r"""https?://[^"'\s<>\\]+?\.mp4(?:\?[^"'\s<>\\]*)?""", content, flags=re.I):
        value = html.unescape(value).replace("\\/", "/").rstrip("),;")
        if direct_mp4(value): return value
    return ""

def duration_from_mp4(url):
    """Probe remote MP4 metadata with ffprobe (HTTP range requests; no full download intended)."""
    if not url or not url.startswith("https://"):
        return ""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-rw_timeout", "15000000",
             "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", url],
            capture_output=True, text=True, timeout=25, check=False
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            if detail:
                log(f"ffprobe duration unavailable: {detail[-1][:180]}")
            return ""
        seconds = float((result.stdout or "").strip())
        if seconds <= 0:
            return ""
        total = int(seconds + 0.5)
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        log(f"ffprobe duration unavailable: {e}")
        return ""

def parse_post(post):
    link = post.get("link", "")
    if not link: return None
    try:
        raw, _ = fetch(link)
        page = raw.decode("utf-8", "replace")
    except Exception as e:
        log(f"SKIP fetch {link}: {e}")
        return None
    video_url = find_video_url(page)
    if not video_url:
        log(f"NO MP4 found: {link}")
        return None
    title_html = (post.get("title") or {}).get("rendered") or post.get("slug") or "Untitled"
    title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", title_html))).strip()
    thumb = meta(page, "og:image") or meta(page, "thumbnailUrl")
    if not thumb:
        media = post.get("_embedded", {}).get("wp:featuredmedia", [])
        if media: thumb = media[0].get("source_url", "")
    date = meta(page, "uploadDate") or post.get("date_gmt") or post.get("date") or ""
    duration = ""
    for key in ("duration", "video:duration", "og:video:duration"):
        duration = parse_duration(meta(page, key))
        if duration: break
    if not duration:
        m = re.search(r"""["']duration["']\s*:\s*["']([^"']+)["']""", page, re.I)
        if m: duration = parse_duration(m.group(1))
    if not duration:
        duration = duration_from_mp4(video_url)
    return {"source_id": str(post.get("id", "")), "title": title, "video_url": video_url,
            "thumb_url": thumb, "date": date, "link": link, "duration": duration}

def encode_url(url, video_id):
    iv = f"HYL{video_id.replace('video', '')}{base64.b32encode(os.urandom(4)).decode('ascii')[:6]}"
    encoded = "".join(chr(ord(c) ^ ord(iv[i % len(iv)])) for i, c in enumerate(url))[::-1]
    return {"provider": "videy", "encrypted": base64.b64encode(encoded.encode("latin1")).decode("ascii"), "iv": iv, "ver": 1}

def random_views():
    # Display-only simulated views, not source analytics.
    n = random.randint(1000, 10000) / 10  # 100.0K to 1,000.0K
    return f"{n/1000:.1f}M" if n >= 1000 else f"{n:.1f}K"

def download_thumb(url, target):
    if not url or not url.startswith("https://"): return False
    try:
        raw, ctype = fetch(url)
        if len(raw) > MAX_THUMB_BYTES or (ctype and not ctype.lower().startswith("image/")): return False
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(raw)) as src:
            im = src.convert("RGB")
            im.thumbnail((960, 720))
            target.parent.mkdir(parents=True, exist_ok=True)
            im.save(target, "WEBP", quality=84, method=4)
        return True
    except Exception as e:
        log(f"Thumbnail unavailable {url}: {e}")
        return False

def empty_catalog():
    DATA.parent.mkdir(parents=True, exist_ok=True)
    for p in THUMBS.glob("*"):
        if p.is_file() and p.suffix.lower() in (".webp", ".jpg", ".jpeg", ".png"):
            p.unlink()
    atomic_json(DATA, {"version":"1.3", "updated":datetime.now(timezone.utc).isoformat(), "videos":[]})
    atomic_json(STATE, {"active":True, "next_page":1, "scanned":0, "created":0})
    log("RESET complete: catalog and thumbnails cleared; import starts at page 1.")

def get_page(page_number):
    url = f"{API}?per_page={PER_PAGE}&page={page_number}&_embed=1&orderby=date&order=desc"
    raw, _ = fetch(url)
    posts = json.loads(raw)
    if not isinstance(posts, list): raise RuntimeError(f"Unexpected WordPress API response on page {page_number}")
    return posts

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="start or continue full import in batches of 100")
    parser.add_argument("--reset", action="store_true", help="DANGER: clear videos.json and thumbnails, then start fresh")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if args.batch_size < 1: raise RuntimeError("--batch-size must be >= 1")
    log("Sync script started.")
    if args.reset:
        empty_catalog()
    if not DATA.exists(): atomic_json(DATA, {"version":"1.3","updated":datetime.now(timezone.utc).isoformat(),"videos":[]})
    db = json.loads(DATA.read_text(encoding="utf-8"))
    if not isinstance(db, dict) or not isinstance(db.get("videos"), list): raise RuntimeError("Catalog JSON must contain a videos array.")
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else None
    if args.full and not state:
        state = {"active":True,"next_page":1,"scanned":0,"created":0}
        atomic_json(STATE, state)
    if state and state.get("active"):
        full = True
    elif args.full:
        full = True
    else:
        full = False
    if not full:
        state = {"active":False,"next_page":1,"scanned":0,"created":0}
        posts = get_page(1)[:args.batch_size]
        start_page = 1
        is_final = True
    else:
        state = state or {"active":True,"next_page":1,"scanned":0,"created":0}
        start_page = int(state.get("next_page",1))
        posts = get_page(start_page)
        if not posts:
            state["active"] = False
            atomic_json(STATE, state)
            log("FULL import complete.")
            return
        posts = posts[:args.batch_size]
        is_final = len(posts) < args.batch_size
    videos = db["videos"]
    known_urls = {decode_url(v.get("video",{})) for v in videos if isinstance(v,dict)}
    known_urls.discard("")
    known_source_ids = {str(v.get("source",{}).get("site_id","")) for v in videos if isinstance(v,dict) and isinstance(v.get("source"),dict)}
    known_source_ids.discard("")
    numeric_ids = [int(m.group(1)) for v in videos if isinstance(v,dict) and (m:=re.fullmatch(r"video(\d+)",str(v.get("id",""))))]
    next_num = max(numeric_ids or [0])+1
    additions = []
    log(f"Processing API page {start_page}: {len(posts)} posts; checkpoint scanned={state.get('scanned',0)}")
    for i, post in enumerate(posts,1):
        sid = str(post.get("id",""))
        if sid and sid in known_source_ids:
            log(f"[{i}/{len(posts)}] SKIP known source post {sid}")
            continue
        log(f"[{i}/{len(posts)}] Checking: {post.get('link','(no link)')}")
        item = parse_post(post)
        if not item: continue
        if item["video_url"] in known_urls:
            log(f"SKIP duplicate video: {item['title']}")
            continue
        video_id = f"video{next_num:03d}"
        thumb_path = THUMBS / f"{video_id}.webp"
        if not download_thumb(item["thumb_url"], thumb_path):
            thumb_path.unlink(missing_ok=True)
            log(f"SKIP no usable thumbnail: {item['title']}")
            continue
        duration = item["duration"]
        if not duration:
            log(f"WARNING: duration could not be detected for {item['title']}; leaving it blank rather than showing a false 00:00.")
        additions.append({"id":video_id,"title":item["title"] or f"Video {next_num}",
            "thumb":f"assets/thumb/{video_id}.webp","views":random_views(),"duration":duration,
            "video":encode_url(item["video_url"],video_id),
            "source":{"site_id":item["source_id"],"url":item["link"],"date":item["date"]}})
        known_urls.add(item["video_url"])
        if sid: known_source_ids.add(sid)
        next_num += 1
        log(f"FOUND new video: {item['title']} (views {additions[-1]['views']}, duration {duration})")
    # Save the batch even if zero videos were found, and persist checkpoint.
    db["videos"] = additions + videos
    db["version"] = str(db.get("version","1.3"))
    db["updated"] = datetime.now(timezone.utc).isoformat()
    atomic_json(DATA, db)
    if full:
        state["scanned"] = int(state.get("scanned",0)) + len(posts)
        state["created"] = int(state.get("created",0)) + len(additions)
        state["next_page"] = start_page + 1
        # A short API page signals end. Exact multiples are followed by an empty page next run.
        if len(posts) < args.batch_size: state["active"] = False
        atomic_json(STATE, state)
        log(f"BATCH SAVED: {len(posts)} posts processed; {len(additions)} videos added. Next API page: {state['next_page']}.")
        if not state["active"]: log(f"FULL import complete: scanned {state['scanned']}, created {state['created']}.")
    else:
        log(f"Recent sync saved: {len(additions)} videos added.")
    log("Sync script finished.")

if __name__ == "__main__":
    try: main()
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr, flush=True)
        raise
