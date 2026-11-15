# ByteVerse Daily — Instagram Autopost

Publishes carousel posts to **@byteverse.daily** automatically at a chosen time,
using the official **Instagram API with Instagram Login** (no Facebook Page needed)
and free **GitHub Actions**.

```
queue/2026-11-17-nodejs/      <- one folder per post
  01.jpg … 10.jpg             <- slides (JPEG, 2–10 of them)
  caption.txt                 <- caption + max 5 hashtags
  post.json                   <- {"publish_at": "2026-11-17T19:30:00+05:30"}
published.json                <- log of what went out (written by the bot)
```

Every 30 minutes the workflow checks the queue and publishes the post whose
`publish_at` has passed (one post per run, never twice — it is logged in `published.json`).

## One-time setup (~20 min)

1. **Instagram** → Settings → *Account type and tools* → **Switch to professional account** (Creator).
2. **developers.facebook.com** → *My Apps* → **Create App** → use case
   *“Manage messaging & content on Instagram”*.
3. In the app: **Instagram → API setup with Instagram login** →
   *Generate access tokens* → **Add account** → log in as @byteverse.daily →
   allow **instagram_business_basic** and **instagram_business_content_publish**.
4. Copy the generated token (it is long-lived, ~60 days). **Don't paste it in any chat.**
5. In this repo: **Settings → Secrets and variables → Actions → New repository secret**
   - `IG_ACCESS_TOKEN` = the token from step 4
   - `GH_PAT` = a fine-grained GitHub token for *this repo only* with
     **Secrets: Read and write** (lets the bot renew the Instagram token by itself)
6. **Actions** tab → *Autopost to Instagram* → **Run workflow** (dry run ✅) — it should
   list the queued post and its image links without posting anything.
7. Optional: *Refresh Instagram token* → **Run workflow** once to check renewal works
   (only succeeds when the token is at least 24 h old).

## Adding a post

```bash
python autopost.py prepare <slides_folder> <caption.txt> 2026-11-20T19:30:00+05:30 2026-11-20-sql
python autopost.py check
git add queue && git commit -m "Queue SQL notes" && git push
```

To cancel a post, delete its folder before its time. To change the time, edit `post.json`.

## Notes
- The repo must be **public**: Instagram downloads the slides from their GitHub link.
- Instagram's API accepts **JPEG only** — `prepare` converts PNGs automatically.
- API carousels hold max **10** slides.
- GitHub may run scheduled jobs a few minutes late; posts go out within ~30–45 min of the set time.
- If anything fails, the run turns red and GitHub emails you. Nothing is marked published unless it really was.
