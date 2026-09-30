# E-Closet Instagram scraper — handoff

Largely vibe coded the scraper

This standalone tool signs into Instagram with a dedicated account, downloads a profile's post images and captions, and writes a portable import folder. Its intended consumer is `E-Closet-APIs`: each Instagram post becomes a reviewable listing draft, not an automatically published listing.

The API work referenced below is an unpushed reference implementation on the `insta-scrape` branch in the E-Closet-APIs git repo. Treat it as product and integration context, not as code to deploy unchanged.

## End-to-end flow

```text
Instagram profile
  -> this scraper (authenticated Playwright session)
  -> output/posts.json + output/images/
  -> E-Closet API import job (one post folder at a time)
  -> Firebase-hosted images + listing drafts
  -> admin review / edit
  -> normal Clothes listing + existing classification/recommendation work
```

The important boundary is the `output/` directory. The scraper does not call E-Closet APIs and should not need E-Closet credentials.

## Setup and first login

Requirements: Python 3 and an Instagram account dedicated to this process. Use an account the team controls; do not put production credentials in source control.

```bash
cd <path-to-this-repository>
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Set credentials as environment variables (preferred):

```bash
export INSTAGRAM_USERNAME='team-account-email-or-username'
export INSTAGRAM_PASSWORD='team-account-password'
.venv/bin/python scrape.py --save-cookies
```

This opens a browser. Complete Instagram's email/SMS/challenge flow there. A successful login saves `playwright_ig_cookies.json`, which later runs reuse. The cookie file is effectively a password: keep it local and rotate/recreate it when it expires. It is gitignored.

Alternatively, log into Instagram in a Chrome instance started with remote debugging and save that session:

```bash
.venv/bin/python scrape.py --save-cookies --cdp http://127.0.0.1:9222
```



## Run the scraper

Edit `PROFILE_URL` near the top of `scrape.py` to the seller profile, then run:

```bash
.venv/bin/python scrape.py
```

Useful options:

```bash
# Choose a destination
.venv/bin/python scrape.py --out /path/to/import-output

# Run without a visible browser (less reliable with Instagram)
.venv/bin/python scrape.py --headless

# Reduce grid pagination attempts while troubleshooting
.venv/bin/python scrape.py --max-scrolls 20
```

The default is intentionally browser-visible and visits posts to recover real captions and all carousel slides. It reuses a saved session or signs in from `INSTAGRAM_USERNAME` / `INSTAGRAM_PASSWORD`; without one of those, it cannot reliably collect a full profile. It throttles requests, pauses periodically, and may still be challenged or rate-limited by Instagram. If that happens, stop, complete/recreate the session, and resume with a conservative run. Login diagnostics are written to `login_debug/` and are gitignored.

## Output contract

Each run writes:

```text
output/
  posts.json
  images/
    ABC123.jpg                 # single-image post
    DEF456/
      01.jpg                   # carousel, in Instagram display order
      02.jpg
```

`posts.json` is the handoff manifest. Its shape is:

```json
{
  "username": "seller_handle",
  "post_count": 2,
  "posts": [{
    "shortcode": "ABC123",
    "url": "https://www.instagram.com/p/ABC123/",
    "caption": "Original Instagram caption",
    "folder": null,
    "files": ["images/ABC123.jpg"]
  }]
}
```

For carousels, `folder` is `images/<shortcode>` and `files` lists every slide in order. `shortcode` is the idempotency key: an API retry should update that same draft rather than create a duplicate. A blank caption or empty `files` array is a scrape-quality exception and should be reviewed before import.

## Intended E-Closet API implementation

The local API branch proposes this import lifecycle:

1. Create an import job for a seller and source profile.
2. For every `posts.json` entry, send its metadata and ordered local images as multipart `images` fields.
3. Store the images under a deterministic Firebase path, retain the caption and extraction result, and create/update one draft keyed by shortcode.
4. Finalize the job after all posts are received. Admins review and complete missing listing fields.
5. Publish only a validated draft through the normal `Clothes` creation path, then trigger existing image-quality, classification, and recommendation work.

The proposed endpoints were:

```text
POST  /admin/instagram-imports
POST  /admin/instagram-imports/:importId/posts
POST  /admin/instagram-imports/:importId/finalize
GET   /admin/instagram-imports/:importId
PATCH /admin/instagram-imports/:importId/drafts/:draftId
POST  /admin/instagram-imports/:importId/drafts/:draftId/publish
```

All use existing admin authentication. The create request identifies the seller (`seller_email` or `seller_id`), the Instagram `profile_url`, and optional category defaults. The per-post request includes `shortcode`, `caption`, `permalink`, and ordered `images`.

The suggested API limits are 1–10 JPEG/PNG/WebP images per post, 10 MiB per image, and 25 MiB total per post. Validate the scraper output before sending it: video or unsupported image formats need a deliberate conversion/skip policy rather than silently failing.

Caption extraction may use an LLM only to copy explicitly stated values (item name, brand, size, color, cleaning, pickup method, and quoted prices). It must never infer attributes from the photos or invent missing fields. Missing data remains a review requirement. Publishing should require the normal essentials: images, seller/category scope, pickup method, required descriptive fields, a price, and any delivery-address checks already enforced by E-Closet.

## API-side helper scripts in the reference implementation

The branch includes two Node scripts that demonstrate the contract. From `E-Closet-APIs`, after implementing/reconciling the endpoints:

```bash
# Check manifest paths, formats, sizes, image count, and captions; no uploads.
npm run instagram:dry-run -- <path-to-scraper-output>

# Create/upload/finalize. Keep the admin token in the environment.
export ECLOSET_API_URL='http://localhost:4030'
export ECLOSET_ADMIN_TOKEN='...'
export ECLOSET_SELLER_EMAIL='seller@example.com'
npm run instagram:import-output -- --output <path-to-scraper-output> \
  --parent-category-id '<mongo-id>' --category-tags '<mongo-id>,<mongo-id>'
```

The uploader can resume a failed import with `--import-id <id>`. It uploads post folders sequentially, so retries are isolated and a duplicate shortcode can safely replace its draft/images.

## Handoff checklist

- Store Instagram credentials/session cookies in a team-approved secret store or controlled machine, never Git or a ticket.
- Port the API sketch selectively onto the current API branch and add endpoint/integration tests before deployment. The local branch contains unrelated worktree/branch differences.
- Start with a small seller profile, run the dry run, import it, and verify carousel order, captions, Firebase URLs, seller scope, draft review, and publish behavior.

