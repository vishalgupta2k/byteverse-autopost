#!/usr/bin/env python3
"""ByteVerse Daily autopost: publish queued Instagram carousels via the
Instagram API with Instagram Login (graph.instagram.com). No Facebook Page needed.

Commands
  prepare <slides_dir> <caption.txt> <publish_at> <slug>   add a post to queue/ (PNG -> JPEG)
  check                                                    validate every queued post
  whoami                                                   show the account the token belongs to
  publish [--dry-run]                                      publish every post whose time has come
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
API = "https://graph.instagram.com/" + os.environ.get("IG_API_VERSION", "v25.0")
IMG_EXT = (".jpg", ".jpeg")
MAX_SLIDES = 10          # API carousel limit
CAPTION_MAX = 2200
HASHTAG_MAX = 5


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def token():
    t = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not t:
        die("IG_ACCESS_TOKEN is not set")
    return t


def call(method, path, **params):
    url = path if path.startswith("http") else f"{API}/{path.lstrip('/')}"
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
        slides = sorted(f for f in d.iterdir() if f.suffix.lower() in IMG_EXT)
        caption = (d / "caption.txt").read_text().strip() if (d / "caption.txt").exists() else ""
        out.append({"slug": d.name, "dir": d, "meta": meta, "slides": slides, "caption": caption})
    return out


def validate(p):
    problems = []
    if not 2 <= len(p["slides"]) <= MAX_SLIDES:
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


def cmd_check():
    log, bad = load_log(), 0
    for p in posts():
        state = "PUBLISHED" if p["slug"] in log else "queued"
        problems = validate(p)
        bad += bool(problems)
        print(f"[{state}] {p['slug']}: {len(p['slides'])} slides, at {p['meta'].get('publish_at')}")
        for msg in problems:
            print(f"    ! {msg}")
    if bad:
        die(f"{bad} post(s) have problems")
    print("queue OK")


def cmd_whoami():
    me = call("GET", "me", fields="user_id,username,account_type,media_count")
    print(json.dumps(me, indent=2))
    lim = call("GET", f"{ig_id()}/content_publishing_limit", fields="quota_usage,config")
    print("publishing limit:", json.dumps(lim.get("data", lim)))


def ig_id():
    env = os.environ.get("IG_USER_ID", "").strip()
    if env:
        return env
    return call("GET", "me", fields="user_id")["user_id"]


def wait_ready(container_id, label):
    for _ in range(30):                       # up to ~5 minutes
        st = call("GET", container_id, fields="status_code").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            die(f"{label} container {container_id} is {st}")
        time.sleep(10)
    die(f"{label} container {container_id} not ready after 5 minutes")


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


def publish_one(p, user_id):
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


def cmd_refresh():
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
    elif cmd == "check":
        cmd_check()
    elif cmd == "whoami":
        cmd_whoami()
    elif cmd == "publish":
        cmd_publish(dry_run="--dry-run" in a)
    elif cmd == "refresh-token":
        cmd_refresh()
    else:
        print(__doc__); sys.exit(2)
