#!/usr/bin/env python3
"""ByteVerse Daily autopost: publish queued Instagram carousels and Reels via the
official Instagram API. No Facebook Page needed.

Commands
  prepare <slides_dir> <caption.txt> <publish_at> <slug>   add a carousel to queue/ (PNG -> JPEG)
  prepare-video <video> <caption.txt> <publish_at> <slug> [cover.png]
                                                           add a Reel to queue/ (re-encodes to 1080x1920 H.264/AAC)
  check                                                    validate every queued post
  whoami                                                   show the account the token belongs to
  publish [--dry-run]                                      publish every post whose time has come
  next-wait                                                seconds until the next post is due (-1 if none)
  refresh-token                                            print a refreshed long-lived token
"""
import datetime as dt
import json
import os
import pathlib
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent
QUEUE = ROOT / "queue"
LOG = ROOT / "published.json"
VER = os.environ.get("IG_API_VERSION", "v25.0")
HOSTS = {"instagram": f"https://graph.instagram.com/{VER}", "facebook": f"https://graph.facebook.com/{VER}"}
_host = {"name": None}
IMG_EXT = (".jpg", ".jpeg")
MAX_SLIDES = 10          # API carousel limit
CAPTION_MAX = 2200
HASHTAG_MAX = 5
VIDEO_MAX_MB = 95        # GitHub rejects files over 100 MB
REEL_MIN_S, REEL_MAX_S = 3, 900


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def token():
    t = os.environ.get("IG_ACCESS_TOKEN", "").strip().strip('"').strip("'")
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    if not t:
        die("IG_ACCESS_TOKEN is not set")
    return t


def host():
    """Instagram-Login tokens use graph.instagram.com, Facebook-Login / Unified-login
    tokens use graph.facebook.com. Pick by token shape, confirm with a probe."""
    if _host["name"]:
        return _host["name"]
    forced = os.environ.get("IG_HOST", "").strip().lower()
    if forced in HOSTS:
        _host["name"] = forced
        return forced
    order = ["facebook", "instagram"] if token().startswith("EAA") else ["instagram", "facebook"]
    errors = []
    for name in order:
        r = requests.get(f"{HOSTS[name]}/me", params={"fields": "id", "access_token": token()}, timeout=60)
        if r.status_code < 400:
            _host["name"] = name
            print(f"token works with graph.{name}.com", file=sys.stderr)
            return name
        errors.append(f"{name}: {r.text[:200]}")
    die("token rejected by both Instagram and Facebook APIs -> " + " | ".join(errors))


def call(method, path, **params):
    url = path if path.startswith("http") else f"{HOSTS[host()]}/{path.lstrip('/')}"
    params["access_token"] = token()
    r = requests.request(method, url, params=params if method == "GET" else None,
                         data=params if method != "GET" else None, timeout=60)
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:300]}
    if r.status_code >= 400 or "error" in body:
        err = body.get("error", body)
        die(f"{method} {path} failed ({r.status_code}): {json.dumps(err)[:500]}")
    return body


# ---------- queue helpers ----------
def load_log():
    return json.loads(LOG.read_text()) if LOG.exists() else {}


def save_log(log):
    LOG.write_text(json.dumps(log, indent=2, sort_keys=True) + "\n")


def parse_time(s):
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise ValueError(f"'{s}' needs a timezone, e.g. 2026-11-17T19:30:00+05:30")
    return t


def posts():
    if not QUEUE.exists():
        return []
    out = []
    for d in sorted(p for p in QUEUE.iterdir() if p.is_dir()):
        meta_f = d / "post.json"
        if not meta_f.exists():
            continue
        meta = json.loads(meta_f.read_text())
        slides = sorted(f for f in d.iterdir() if f.suffix.lower() in IMG_EXT and f.stem != "cover")
        caption = (d / "caption.txt").read_text().strip() if (d / "caption.txt").exists() else ""
        video = d / "video.mp4"
        cover = d / "cover.jpg"
        kind = "reel" if video.exists() else "carousel"
        out.append({"slug": d.name, "dir": d, "meta": meta, "slides": slides, "caption": caption, "kind": kind,
                    "video": video if video.exists() else None, "cover": cover if cover.exists() else None})
    return out


def probe(video):
    """Duration / codecs via ffprobe, if installed. Returns {} when unavailable."""
    import shutil, subprocess
    if not shutil.which("ffprobe"):
        return {}
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
                        "-of", "json", str(video)], capture_output=True, text=True)
    try:
        info = json.loads(r.stdout)
    except ValueError:
        return {"error": r.stderr.strip()[:200] or "unreadable video"}
    out = {"duration": float(info.get("format", {}).get("duration", 0) or 0)}
    for st in info.get("streams", []):
        if st.get("codec_type") == "video":
            out.update(vcodec=st.get("codec_name"), width=st.get("width"), height=st.get("height"))
        elif st.get("codec_type") == "audio":
            out["acodec"] = st.get("codec_name")
    return out


def validate_reel(p):
    problems = []
    mb = p["video"].stat().st_size / 1024 / 1024
    if mb > VIDEO_MAX_MB:
        problems.append(f"video.mp4 is {mb:.0f} MB (max {VIDEO_MAX_MB} MB) - re-run prepare-video")
    info = probe(p["video"])
    if "error" in info:
        problems.append(f"video.mp4 can't be read: {info['error']}")
    elif info:
        if not REEL_MIN_S <= info["duration"] <= REEL_MAX_S:
            problems.append(f"video is {info['duration']:.1f}s (Reels need {REEL_MIN_S}s-{REEL_MAX_S // 60} min)")
        if info.get("vcodec") not in ("h264", "hevc"):
            problems.append(f"video codec {info.get('vcodec')} (use H.264) - re-run prepare-video")
        if info.get("acodec") not in (None, "aac"):
            problems.append(f"audio codec {info.get('acodec')} (use AAC) - re-run prepare-video")
    if p["slides"]:
        problems.append("a Reel folder must not also contain slides (only video.mp4 + optional cover.jpg)")
    return problems


def validate(p):
    problems = []
    if p["kind"] == "reel":
        problems += validate_reel(p)
    elif not 2 <= len(p["slides"]) <= MAX_SLIDES:
        problems.append(f"needs 2-{MAX_SLIDES} JPEG slides, found {len(p['slides'])}")
    if not p["caption"]:
        problems.append("caption.txt is missing or empty")
    if len(p["caption"]) > CAPTION_MAX:
        problems.append(f"caption is {len(p['caption'])} chars (max {CAPTION_MAX})")
    tags = [w for w in p["caption"].split() if w.startswith("#")]
    if len(tags) > HASHTAG_MAX:
        problems.append(f"{len(tags)} hashtags (Instagram allows {HASHTAG_MAX})")
    try:
        parse_time(p["meta"]["publish_at"])
    except (KeyError, ValueError) as e:
        problems.append(f"bad publish_at: {e}")
    for s in p["slides"]:
        if s.stat().st_size > 8 * 1024 * 1024:
            problems.append(f"{s.name} is over 8 MB")
    return problems


# ---------- commands ----------
def cmd_prepare(slides_dir, caption_file, publish_at, slug):
    from PIL import Image
    try:
        parse_time(publish_at)
    except ValueError as e:
        die(str(e))
    src = pathlib.Path(slides_dir)
    pngs = sorted(f for f in src.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg"))
    if not pngs:
        die(f"no images in {src}")
    dest = QUEUE / slug
    dest.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(pngs, 1):
        im = Image.open(f).convert("RGB")
        im.save(dest / f"{i:02d}.jpg", "JPEG", quality=92, optimize=True, progressive=False)
    (dest / "caption.txt").write_text(pathlib.Path(caption_file).read_text().strip() + "\n")
    (dest / "post.json").write_text(json.dumps({"publish_at": publish_at}, indent=2) + "\n")
    print(f"queued {slug}: {len(pngs)} slides, publishes at {publish_at}")
    return cmd_check()


def cmd_prepare_video(video, caption_file, publish_at, slug, cover=None):
    """Re-encode any video into Instagram's Reels spec and queue it."""
    import shutil, subprocess
    try:
        parse_time(publish_at)
    except ValueError as e:
        die(str(e))
    if not shutil.which("ffmpeg"):
        die("ffmpeg is needed for prepare-video")
    src = pathlib.Path(video)
    if not src.exists():
        die(f"{src} not found")
    dest = QUEUE / slug
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "video.mp4"
    vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
          "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x060a18,fps=30,format=yuv420p")
    has_audio = probe(src).get("acodec") is not None if shutil.which("ffprobe") else True
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    if not has_audio:   # Instagram is happier with an audio track; add silence
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"]
    for crf in (21, 24, 27, 30):
        subprocess.run(cmd + ["-vf", vf, "-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast",
                              "-crf", str(crf), "-maxrate", "8M", "-bufsize", "16M",
                              "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart",
                              str(out)], check=True)
        if out.stat().st_size / 1048576 <= VIDEO_MAX_MB:
            break
    if cover:
        from PIL import Image
        Image.open(cover).convert("RGB").save(dest / "cover.jpg", "JPEG", quality=92)
    (dest / "caption.txt").write_text(pathlib.Path(caption_file).read_text().strip() + "\n")
    (dest / "post.json").write_text(json.dumps({"publish_at": publish_at, "share_to_feed": True}, indent=2) + "\n")
    print(f"queued Reel {slug}: {out.stat().st_size / 1048576:.1f} MB, publishes at {publish_at}")
    return cmd_check()


def cmd_check():
    log, bad = load_log(), 0
    for p in posts():
        state = "PUBLISHED" if p["slug"] in log else "queued"
        problems = validate(p)
        bad += bool(problems)
        what = (f"Reel ({p['video'].stat().st_size / 1048576:.1f} MB{', cover' if p['cover'] else ''})"
                if p["kind"] == "reel" else f"{len(p['slides'])} slides")
        print(f"[{state}] {p['slug']}: {what}, at {p['meta'].get('publish_at')}")
        for msg in problems:
            print(f"    ! {msg}")
    if bad:
        die(f"{bad} post(s) have problems")
    print("queue OK")


def cmd_whoami():
    if host() == "instagram":
        me = call("GET", "me", fields="user_id,username,account_type,media_count")
    else:
        me = call("GET", ig_id(), fields="username,media_count")
    print(json.dumps(me, indent=2))
    lim = call("GET", f"{ig_id()}/content_publishing_limit", fields="quota_usage,config")
    print("publishing limit:", json.dumps(lim.get("data", lim)))


def ig_id():
    env = os.environ.get("IG_USER_ID", "").strip()
    if env:
        return env
    if host() == "facebook":
        die("IG_USER_ID is required with a Facebook-style token")
    return call("GET", "me", fields="user_id")["user_id"]


def wait_ready(container_id, label, minutes=5):
    for _ in range(minutes * 6):              # check every 10 s
        info = call("GET", container_id, fields="status_code,status")
        st = info.get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            die(f"{label} container {container_id} is {st}: {info.get('status', '')}")
        time.sleep(10)
    die(f"{label} container {container_id} not ready after {minutes} minutes")


def public_url(path):
    repo = os.environ.get("GITHUB_REPOSITORY")
    sha = os.environ.get("GITHUB_SHA")
    base = os.environ.get("IMAGE_BASE_URL")
    rel = path.relative_to(ROOT).as_posix()
    if base:
        return f"{base.rstrip('/')}/{rel}"
    if repo and sha:
        return f"https://raw.githubusercontent.com/{repo}/{sha}/{rel}"
    die("can't build public image URLs: set IMAGE_BASE_URL or run inside GitHub Actions")


def upload_video(container, video):
    """Resumable upload: send the file bytes straight to Meta (no public link needed)."""
    uri = container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{VER}/{container['id']}"
    size = video.stat().st_size
    with open(video, "rb") as f:
        r = requests.post(uri, data=f, timeout=900, headers={
            "Authorization": f"OAuth {token()}", "offset": "0", "file_size": str(size)})
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:300]}
    if r.status_code >= 400 or not body.get("success", True) or "error" in body:
        die(f"video upload failed ({r.status_code}): {json.dumps(body)[:500]}")
    print(f"  uploaded {size / 1048576:.1f} MB")


def publish_reel(p, user_id):
    params = {"media_type": "REELS", "upload_type": "resumable", "caption": p["caption"],
              "share_to_feed": "true" if p["meta"].get("share_to_feed", True) else "false"}
    if p["cover"]:
        params["cover_url"] = public_url(p["cover"])
    elif "thumb_offset_ms" in p["meta"]:
        params["thumb_offset"] = str(int(p["meta"]["thumb_offset_ms"]))
    c = call("POST", f"{user_id}/media", **params)
    print(f"  reel container {c['id']}")
    upload_video(c, p["video"])
    wait_ready(c["id"], "reel", minutes=15)   # Instagram transcodes the video first
    res = call("POST", f"{user_id}/media_publish", creation_id=c["id"])
    media_id = res["id"]
    link = call("GET", media_id, fields="permalink").get("permalink", "")
    return media_id, link


def publish_one(p, user_id):
    if p["kind"] == "reel":
        return publish_reel(p, user_id)
    children = []
    for s in p["slides"]:
        c = call("POST", f"{user_id}/media", image_url=public_url(s), is_carousel_item="true")
        children.append(c["id"])
        print(f"  item {s.name} -> container {c['id']}")
    for cid in children:
        wait_ready(cid, "item")
    car = call("POST", f"{user_id}/media", media_type="CAROUSEL", children=",".join(children), caption=p["caption"])
    print(f"  carousel container {car['id']}")
    wait_ready(car["id"], "carousel")
    res = call("POST", f"{user_id}/media_publish", creation_id=car["id"])
    media_id = res["id"]
    link = call("GET", media_id, fields="permalink").get("permalink", "")
    return media_id, link


def cmd_publish(dry_run=False):
    now = dt.datetime.now(dt.timezone.utc)
    log = load_log()
    pending = [p for p in posts() if p["slug"] not in log]
    for p in pending:
        problems = validate(p)
        if problems:
            die(f"{p['slug']}: " + "; ".join(problems))
    due = [p for p in pending if parse_time(p["meta"]["publish_at"]) <= now]
    if not due:
        print("nothing due")
        return
    if dry_run:
        for p in due:
            if p["kind"] == "reel":
                print(f"[dry-run] would publish Reel {p['slug']} ({p['video'].stat().st_size / 1048576:.1f} MB, direct upload)")
                if p["cover"]:
                    print("    cover:", public_url(p["cover"]))
                continue
            print(f"[dry-run] would publish {p['slug']} ({len(p['slides'])} slides)")
            for s in p["slides"]:
                print("   ", public_url(s))
        return
    user_id = ig_id()
    for p in due[:1]:                         # one post per run: never floods the feed
        print(f"publishing {p['slug']} ...")
        media_id, link = publish_one(p, user_id)
        log[p["slug"]] = {"media_id": media_id, "permalink": link,
                          "published_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        save_log(log)
        print(f"published {p['slug']}: {link}")
    if len(due) > 1:
        print(f"{len(due) - 1} more due post(s) will go out on the next runs")


def cmd_next_wait():
    """Print seconds until the earliest unpublished post is due (0 if due now, -1 if none)."""
    log = load_log()
    times = [parse_time(p["meta"]["publish_at"]) for p in posts() if p["slug"] not in log]
    if not times:
        print(-1); return
    secs = (min(times) - dt.datetime.now(dt.timezone.utc)).total_seconds()
    print(max(0, int(secs)))


def cmd_refresh():
    if host() == "facebook":
        app_id, secret = os.environ.get("FB_APP_ID", ""), os.environ.get("FB_APP_SECRET", "")
        if not (app_id and secret):
            print("Facebook-style token: add FB_APP_ID + FB_APP_SECRET secrets to auto-refresh it", file=sys.stderr)
            sys.exit(3)
        r = requests.get(f"{HOSTS['facebook']}/oauth/access_token", timeout=60, params={
            "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": secret, "fb_exchange_token": token()})
        body = r.json()
        if "access_token" not in body:
            die(f"refresh failed: {json.dumps(body)[:400]}")
        print("refreshed Facebook-style token", file=sys.stderr)
        print(body["access_token"])
        return
    r = requests.get("https://graph.instagram.com/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": token()}, timeout=60)
    body = r.json()
    if "access_token" not in body:
        die(f"refresh failed: {json.dumps(body)[:400]}")
    days = body.get("expires_in", 0) // 86400
    print(f"refreshed: valid for ~{days} days", file=sys.stderr)
    print(body["access_token"])               # stdout = token only (piped into gh secret set)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__); sys.exit(0)
    cmd = a[0]
    if cmd == "prepare" and len(a) == 5:
        cmd_prepare(*a[1:])
    elif cmd == "prepare-video" and len(a) in (5, 6):
        cmd_prepare_video(*a[1:])
    elif cmd == "check":
        cmd_check()
    elif cmd == "whoami":
        cmd_whoami()
    elif cmd == "publish":
        cmd_publish(dry_run="--dry-run" in a)
    elif cmd == "next-wait":
        cmd_next_wait()
    elif cmd == "refresh-token":
        cmd_refresh()
    else:
        print(__doc__); sys.exit(2)
