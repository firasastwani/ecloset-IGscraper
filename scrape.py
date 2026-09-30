#!/usr/bin/env python3
"""Scrape Instagram post images and captions with Playwright."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Response, TimeoutError as PlaywrightTimeout, sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("scrape")

# Change this to the profile you want to scrape.
PROFILE_URL = "https://www.instagram.com/helenatrentals/"

# Scraper account. Leave blank to use INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD env vars.
INSTAGRAM_USERNAME = ""
INSTAGRAM_PASSWORD = ""

# Ban-avoidance timings, aligned with the Instaloader rate-control conventions.
QUERY_DELAY_RANGE = (8.0, 15.0)
POST_DELAY_RANGE = (3.0, 5.0)
LOGIN_RETRY_WAIT = 60
LOGIN_MAX_ATTEMPTS = 3
BREAK_EVERY_N_POSTS = 100
BREAK_SECONDS = 60

ROOT = Path(__file__).resolve().parent
COOKIE_FILE = ROOT / "playwright_ig_cookies.json"
LOGIN_DEBUG_DIR = ROOT / "login_debug"
SHORTCODE_RE = re.compile(r"/(?:p|reel)/([A-Za-z0-9_-]+)/?")
PLACEHOLDER_CAPTION_RE = re.compile(
    r"^(Photo by |Photo shared by |May be an image of)",
    re.I,
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""
# Instagram currently uses email/pass; older pages used username/password.
USERNAME_SELECTORS = (
    "input[name='email']",
    "input[name='username']",
    "input[autocomplete*='username']",
    "#login_form input[type='text']",
)
PASSWORD_SELECTORS = (
    "input[name='pass']",
    "input[name='password']",
    "input[type='password']",
    "#login_form input[type='password']",
)
LOGIN_BUTTON_SELECTORS = (
    "div[role='button'][aria-label='Log In']",
    "div[role='button'][aria-label='Log in']",
    "#login_form div[role='button']",
    "button[type='submit']",
    "form button",
    "input[type='submit']",
)


@dataclass
class Post:
    shortcode: str
    caption: str = ""
    image_urls: list[str] = field(default_factory=list)
    local_files: list[str] = field(default_factory=list)
    folder: str | None = None

    def merge(self, other: Post) -> None:
        self.caption = prefer_caption(self.caption, other.caption)
        self.image_urls = merge_image_urls(self.image_urls, other.image_urls)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull Instagram post images and captions via Playwright."
    )
    parser.add_argument("--out", default=str(ROOT / "output"), help="Output directory.")
    parser.add_argument(
        "--cookies",
        default=str(COOKIE_FILE),
        help="Cookie JSON saved after login (default: playwright_ig_cookies.json).",
    )
    parser.add_argument("--headless", action="store_true", help="Run Chromium headless (often blocked).")
    parser.add_argument(
        "--max-scrolls",
        type=int,
        default=200,
        help="Max grid scroll attempts while loading every post.",
    )
    parser.add_argument(
        "--visit-posts",
        action="store_true",
        default=True,
        help="Open each post to get the real caption and carousel slides (default: on).",
    )
    parser.add_argument("--no-visit-posts", action="store_false", dest="visit_posts")
    parser.add_argument(
        "--save-cookies",
        action="store_true",
        help="Open a browser, wait until you finish login (including the email code), "
        "save cookies, and exit.",
    )
    parser.add_argument(
        "--cdp",
        help="Pull cookies from an already-logged-in Chrome via DevTools, "
        "e.g. http://127.0.0.1:9222. Use with --save-cookies.",
    )
    return parser.parse_args()


def is_placeholder_caption(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    return bool(PLACEHOLDER_CAPTION_RE.search(stripped))


def prefer_caption(old: str, new: str) -> str:
    real = [text.strip() for text in (new, old) if text and not is_placeholder_caption(text)]
    if real:
        return max(real, key=len)
    return (old or new or "").strip()


def image_id(url: str) -> str:
    return urlparse(url).path.rsplit("/", 1)[-1]


def image_quality(url: str) -> int:
    match = re.search(r"[_/][se](\d+)x(\d+)", url)
    if match:
        return int(match.group(1)) * int(match.group(2))
    if "scontent" in url:
        return 1
    return 0


def is_post_image(url: str) -> bool:
    if not isinstance(url, str) or not url.startswith("http"):
        return False
    if "scontent" not in url and "cdninstagram" not in url:
        return False
    path = urlparse(url).path
    if "-19/" in path or "/t51.2885-19/" in path:
        return False
    if any(token in url for token in ("s150x150", "s240x240", "p150x150")):
        return False
    return True


def merge_image_urls(old: list[str], new: list[str]) -> list[str]:
    by_id: dict[str, str] = {}
    order: list[str] = []

    def add(url: str) -> None:
        if not is_post_image(url):
            return
        key = image_id(url)
        if key not in by_id:
            order.append(key)
            by_id[key] = url
        elif image_quality(url) >= image_quality(by_id[key]):
            by_id[key] = url

    primary, secondary = (new, old) if len(new) > len(old) else (old, new)
    for url in primary:
        add(url)
    for url in secondary:
        add(url)
    return [by_id[key] for key in order]


def username_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    username = path.split("/")[0] if path else ""
    if not re.fullmatch(r"[A-Za-z0-9._]+", username or ""):
        raise SystemExit(f"Could not parse a profile username from PROFILE_URL: {url}")
    return username


def shortcode_from_page_url(url: str) -> str | None:
    match = SHORTCODE_RE.search(url)
    return match.group(1) if match else None


def media_owner_username(node: dict[str, Any]) -> str | None:
    for key in ("user", "owner"):
        value = node.get(key)
        if isinstance(value, dict):
            username = value.get("username")
            if isinstance(username, str) and username:
                return username
    return None


def collect_stats(payload: Any, stats: dict[str, Any]) -> None:
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key in ("media_count", "all_media_count"):
                value = current.get(key)
                if isinstance(value, int) and value > 0:
                    stats["media_count"] = max(stats.get("media_count") or 0, value)
            timeline = current.get("edge_owner_to_timeline_media")
            if isinstance(timeline, dict) and isinstance(timeline.get("count"), int):
                stats["media_count"] = max(stats.get("media_count") or 0, timeline["count"])
            page_info = current.get("page_info")
            if isinstance(page_info, dict) and "has_next_page" in page_info:
                stats["has_next_page"] = bool(page_info.get("has_next_page"))
                if page_info.get("end_cursor"):
                    stats["end_cursor"] = page_info.get("end_cursor")
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def parse_json_body(text: str) -> Any | None:
    text = text.strip()
    if text.startswith("for (;;);"):
        text = text[9:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def best_image_url(node: dict[str, Any]) -> str | None:
    candidates = (node.get("image_versions2") or {}).get("candidates") or []
    if candidates:
        ranked = sorted(candidates, key=lambda item: item.get("width") or 0, reverse=True)
        url = ranked[0].get("url")
        if url:
            return url
    for key in ("display_url", "display_uri", "thumbnail_src", "thumbnail_url"):
        url = node.get(key)
        if isinstance(url, str) and url.startswith("http"):
            return url
    return None


def caption_from_node(node: dict[str, Any]) -> str:
    caption = node.get("caption")
    if isinstance(caption, str) and not is_placeholder_caption(caption):
        return caption.strip()
    if isinstance(caption, dict):
        text = caption.get("text") or caption.get("caption")
        if isinstance(text, str) and not is_placeholder_caption(text):
            return text.strip()
    edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
    if edges:
        text = (edges[0].get("node") or {}).get("text")
        if isinstance(text, str) and not is_placeholder_caption(text):
            return text.strip()
    return ""


def shortcode_from_node(node: dict[str, Any]) -> str | None:
    for key in ("code", "shortcode"):
        value = node.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{5,}", value):
            return value
    url = node.get("url") or node.get("permalink") or ""
    if isinstance(url, str):
        match = SHORTCODE_RE.search(url)
        if match:
            return match.group(1)
    return None


def image_urls_from_node(node: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    carousel = node.get("carousel_media") or []
    if not carousel:
        sidecar = (node.get("edge_sidecar_to_children") or {}).get("edges") or []
        carousel = [edge.get("node") or {} for edge in sidecar]

    sources = carousel if carousel else [node]
    for child in sources:
        if not isinstance(child, dict):
            continue
        url = best_image_url(child)
        if url and is_post_image(url) and url not in urls:
            urls.append(url)
    return urls


def looks_like_media(node: dict[str, Any]) -> bool:
    if node.get("product_type") == "carousel_item":
        return False
    if shortcode_from_node(node) is None:
        return False
    return any(
        key in node
        for key in (
            "image_versions2",
            "display_url",
            "display_uri",
            "carousel_media",
            "edge_sidecar_to_children",
            "video_versions",
            "thumbnail_src",
        )
    )


def walk_posts(
    payload: Any,
    posts: dict[str, Post],
    allow_new: bool = True,
    owner: str | None = None,
) -> None:
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if looks_like_media(current):
                code = shortcode_from_node(current)
                media_owner = media_owner_username(current)
                if code and (not owner or not media_owner or media_owner == owner):
                    post = Post(
                        shortcode=code,
                        caption=caption_from_node(current),
                        image_urls=image_urls_from_node(current),
                    )
                    if code in posts:
                        posts[code].merge(post)
                    elif allow_new:
                        posts[code] = post
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def credentials() -> tuple[str, str]:
    username = (os.environ.get("INSTAGRAM_USERNAME") or INSTAGRAM_USERNAME).strip()
    password = (os.environ.get("INSTAGRAM_PASSWORD") or INSTAGRAM_PASSWORD).strip()
    return username, password


def pause(page: Page | None, low: float, high: float, reason: str) -> None:
    delay = random.uniform(low, high)
    logger.info("Waiting %.2fs before %s", delay, reason)
    if page is not None:
        page.wait_for_timeout(int(delay * 1000))
    else:
        time.sleep(delay)


def login_blocked(page: Page) -> str | None:
    url = page.url.lower()
    if any(token in url for token in ("checkpoint", "challenge", "two_factor", "two-factor", "auth_platform")):
        return "checkpoint"
    try:
        if page.locator('input[name="verificationCode"]').first.is_visible(timeout=500):
            return "two_factor"
    except Exception:
        pass
    return None


def login_error_text(page: Page) -> str:
    for selector in ('#slfErrorAlert', 'p[role="alert"]', '[id*="error"]'):
        loc = page.locator(selector).first
        try:
            if loc.is_visible(timeout=400):
                text = (loc.inner_text() or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


def login_locator(
    page: Page,
    selectors: tuple[str, ...],
    role: str | None = None,
    name: re.Pattern[str] | None = None,
    timeout: int = 45_000,
) -> Any:
    deadline = time.monotonic() + timeout / 1000
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        dismiss_banners(page)
        if role and name:
            try:
                labeled = page.get_by_role(role, name=name).first
                if labeled.is_visible(timeout=250):
                    return labeled
            except Exception as exc:
                last_error = exc
        for selector in selectors:
            loc = page.locator(selector).first
            try:
                if loc.count() and loc.is_visible(timeout=250):
                    return loc
            except Exception as exc:
                last_error = exc
        page.wait_for_timeout(300)
    raise PlaywrightTimeout(
        f"Login field not found. Tried: {', '.join(selectors)}"
    ) from last_error


def fill_visible(
    page: Page,
    selectors: tuple[str, ...],
    value: str,
    role: str | None = None,
    name: re.Pattern[str] | None = None,
) -> None:
    loc = login_locator(page, selectors, role=role, name=name)
    loc.click()
    loc.fill("")
    loc.press_sequentially(value, delay=random.randint(40, 110))


def click_login_button(page: Page) -> None:
    loc = login_locator(
        page,
        LOGIN_BUTTON_SELECTORS,
        role="button",
        name=re.compile(r"^log\s*in$", re.I),
        timeout=15_000,
    )
    loc.click()


def dump_login_debug(page: Page, attempt: int) -> None:
    LOGIN_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    screenshot = LOGIN_DEBUG_DIR / f"login_fail_{attempt}.png"
    html_path = LOGIN_DEBUG_DIR / f"login_fail_{attempt}.html"
    try:
        page.screenshot(path=str(screenshot), full_page=True)
    except Exception:
        screenshot = None
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception:
        html_path = None
    logger.error(
        "Login debug saved (%s) url=%s",
        ", ".join(str(path) for path in (screenshot, html_path) if path),
        page.url,
    )


def is_logged_in(context: Any) -> bool:
    names = {cookie.get("name") for cookie in instagram_cookies(context)}
    return "sessionid" in names or "ds_user_id" in names


def instagram_cookies(context: Any) -> list[dict[str, Any]]:
    cookies = list(context.cookies())
    filtered = [cookie for cookie in cookies if "instagram.com" in (cookie.get("domain") or "")]
    return filtered or cookies


def save_cookies(context: Any, path: Path) -> None:
    path.write_text(json.dumps(instagram_cookies(context), indent=2), encoding="utf-8")
    logger.info("Cookies saved to %s", path)


def normalize_cookie(raw: dict[str, Any]) -> dict[str, Any]:
    same_site_raw = str(raw.get("sameSite") or raw.get("same_site") or "")
    same_site = {
        "no_restriction": "None",
        "unspecified": "Lax",
        "none": "None",
        "lax": "Lax",
        "strict": "Strict",
    }.get(same_site_raw.lower())
    cookie: dict[str, Any] = {
        "name": raw["name"],
        "value": raw.get("value") or "",
        "domain": raw.get("domain") or ".instagram.com",
        "path": raw.get("path") or "/",
        "httpOnly": bool(raw.get("httpOnly", raw.get("http_only", False))),
        "secure": bool(raw.get("secure", True)),
    }
    expires = raw.get("expires", raw.get("expirationDate"))
    if isinstance(expires, (int, float)) and expires > 0:
        cookie["expires"] = int(expires)
    if same_site:
        cookie["sameSite"] = same_site
    return cookie


def load_cookies(context: Any, path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("cookies") or payload.get("Cookies") or []
    cookies = [normalize_cookie(item) for item in payload if isinstance(item, dict) and item.get("name")]
    context.add_cookies(cookies)
    logger.info("Loaded cookies from %s", path)


def wait_for_manual_login(page: Page, timeout_s: int = 600) -> bool:
    logger.info(
        "Enter the email/SMS code in the browser window if Instagram asks. "
        "Waiting up to %ss for a session cookie...",
        timeout_s,
    )
    deadline = time.monotonic() + timeout_s
    last_log = 0.0
    while time.monotonic() < deadline:
        dismiss_banners(page)
        if is_logged_in(page.context) and not login_blocked(page):
            return True
        remaining = int(deadline - time.monotonic())
        if time.monotonic() - last_log >= 15:
            logger.info("Still waiting for login... %ss left (url=%s)", remaining, page.url)
            last_log = time.monotonic()
        page.wait_for_timeout(2000)
    return is_logged_in(page.context)


def session_is_valid(page: Page) -> bool:
    if not is_logged_in(page.context):
        return False
    pause(page, *QUERY_DELAY_RANGE, "session check")
    page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(2000)
    dismiss_banners(page)
    if "login" in page.url.lower() or "challenge" in page.url.lower():
        return False
    return is_logged_in(page.context)


def login_instagram(page: Page, username: str, password: str, cookie_path: Path) -> bool:
    for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        try:
            logger.info("Logging in as %s (attempt %s/%s)", username, attempt, LOGIN_MAX_ATTEMPTS)
            page.context.clear_cookies()
            page.goto(
                "https://www.instagram.com/accounts/login/",
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            page.wait_for_timeout(2000)
            dismiss_banners(page)
            fill_visible(
                page,
                USERNAME_SELECTORS,
                username,
                role="textbox",
                name=re.compile(r"(mobile number|phone number|username|email)", re.I),
            )
            fill_visible(
                page,
                PASSWORD_SELECTORS,
                password,
                role="textbox",
                name=re.compile(r"^password$", re.I),
            )
            page.wait_for_timeout(800)
            click_login_button(page)
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
            except PlaywrightTimeout:
                logger.warning("networkidle timed out after login; continuing")
            page.wait_for_timeout(5000)
            dismiss_banners(page)

            blocked = login_blocked(page)
            if blocked:
                logger.warning(
                    "Instagram %s required. Keep this browser window open and finish the email/SMS code.",
                    blocked,
                )
                if not wait_for_manual_login(page):
                    dump_login_debug(page, attempt)
                    logger.error(
                        "Timed out waiting for the checkpoint. Re-run with --save-cookies and complete login there."
                    )
                    return False
            if not is_logged_in(page.context):
                dump_login_debug(page, attempt)
                error = login_error_text(page)
                raise RuntimeError(error or "login did not produce a session cookie")

            save_cookies(page.context, cookie_path)
            logger.info("Successfully logged in as %s", username)
            return True
        except Exception as exc:
            logger.error("Login failed (attempt %s): %s", attempt, exc)
            try:
                dump_login_debug(page, attempt)
            except Exception:
                pass
            if attempt < LOGIN_MAX_ATTEMPTS:
                logger.info("Waiting %ss before retrying login", LOGIN_RETRY_WAIT)
                time.sleep(LOGIN_RETRY_WAIT)
    logger.error("Login failed after %s attempts", LOGIN_MAX_ATTEMPTS)
    return False


def dismiss_banners(page: Page) -> None:
    for selector in (
        'button:has-text("Allow all cookies")',
        'button:has-text("Allow essential and optional cookies")',
        'button:has-text("Decline optional cookies")',
        'button:has-text("Only allow essential cookies")',
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        'button:has-text("Allow all")',
        'button:has-text("Not Now")',
        'button:has-text("Not now")',
        '[aria-label="Close"]',
    ):
        button = page.locator(selector).first
        try:
            if button.is_visible(timeout=800):
                button.click(timeout=1000)
                page.wait_for_timeout(400)
        except Exception:
            continue


def collect_from_dom(page: Page, posts: dict[str, Post]) -> None:
    anchors = page.locator('a[href*="/p/"], a[href*="/reel/"]').all()
    for anchor in anchors:
        href = anchor.get_attribute("href") or ""
        match = SHORTCODE_RE.search(href)
        if not match:
            continue
        code = match.group(1)
        img = anchor.locator("img").first
        src = None
        try:
            if img.count():
                src = img.get_attribute("src")
        except Exception:
            pass
        post = posts.setdefault(code, Post(shortcode=code))
        if src and is_post_image(src):
            post.image_urls = merge_image_urls(post.image_urls, [src])


def attach_network_listener(
    page: Page,
    posts: dict[str, Post],
    options: dict[str, Any],
) -> None:
    interesting = (
        "graphql/query",
        "/graphql/",
        "/api/v1/feed/",
        "/api/v1/media/",
        "query_id=",
        "doc_id=",
    )

    def on_response(response: Response) -> None:
        url = response.url
        if not any(token in url for token in interesting):
            return
        if response.status != 200:
            return
        try:
            text = response.text()
        except Exception:
            return
        payload = parse_json_body(text)
        if payload is None:
            return
        collect_stats(payload, options["stats"])
        walk_posts(
            payload,
            posts,
            allow_new=options.get("accept_new", True),
            owner=options.get("owner"),
        )

    page.on("response", on_response)


def wait_for_grid(page: Page) -> None:
    print("Waiting for the post grid...")
    page.wait_for_selector('a[href*="/p/"], a[href*="/reel/"]', timeout=120_000)


def expected_post_count(page: Page, stats: dict[str, Any]) -> int | None:
    for selector in ('meta[name="description"]', 'meta[property="og:description"]'):
        try:
            content = page.locator(selector).first.get_attribute("content") or ""
        except Exception:
            content = ""
        match = re.search(r"([\d,]+)\s+Posts", content, re.I)
        if match:
            return int(match.group(1).replace(",", ""))
    try:
        header = page.locator("header").inner_text(timeout=3000)
        match = re.search(r"([\d,]+)\s+posts", header, re.I)
        if match:
            return int(match.group(1).replace(",", ""))
    except Exception:
        pass
    if stats.get("media_count"):
        return int(stats["media_count"])
    return None


def scroll_to_bottom(page: Page) -> None:
    page.evaluate(
        """
        () => {
          const root = document.scrollingElement || document.documentElement;
          root.scrollTo(0, root.scrollHeight);
          window.scrollTo(0, document.body.scrollHeight);
          const main = document.querySelector('main');
          if (main) main.scrollTo(0, main.scrollHeight);
          for (const el of document.querySelectorAll('div')) {
            const style = getComputedStyle(el);
            if (
              (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
              el.scrollHeight > el.clientHeight + 80
            ) {
              el.scrollTop = el.scrollHeight;
            }
          }
        }
        """
    )


def scroll_grid(
    page: Page,
    posts: dict[str, Post],
    max_scrolls: int,
    expected: int | None,
) -> None:
    last_count = 0
    stagnant = 0
    print(
        f"Scrolling the grid to load every post"
        + (f" ({expected} expected)..." if expected else "...")
    )
    for index in range(max_scrolls):
        dismiss_banners(page)
        collect_from_dom(page, posts)
        if expected and len(posts) >= expected:
            print(f"  loaded all {len(posts)} posts")
            break
        try:
            with page.expect_response(
                lambda response: any(
                    token in response.url
                    for token in ("graphql/query", "/api/v1/feed/", "doc_id=")
                )
                and response.status == 200,
                timeout=4000,
            ):
                scroll_to_bottom(page)
        except PlaywrightTimeout:
            pause(page, *QUERY_DELAY_RANGE, "grid scroll")
        else:
            pause(page, *QUERY_DELAY_RANGE, "next grid page")
        collect_from_dom(page, posts)
        count = len(posts)
        print(f"  scroll {index + 1}/{max_scrolls}: {count} posts")
        if count == last_count:
            stagnant += 1
            limit = 6 if expected and count < expected else 4
            if stagnant >= limit:
                break
        else:
            stagnant = 0
            last_count = count


def caption_from_og(description: str | None, username: str) -> str:
    if not description:
        return ""
    prefix = f'{username} on Instagram: "'
    caption = ""
    if description.startswith(prefix) and description.endswith('"'):
        caption = description[len(prefix) : -1]
    else:
        match = re.search(r'on Instagram:\s*[“"](.*)[”"]\s*$', description, flags=re.S)
        if match:
            caption = match.group(1)
    caption = html.unescape(caption.replace("\\n", "\n")).strip()
    return "" if is_placeholder_caption(caption) else caption


def harvest_page_json(page: Page, posts: dict[str, Post], username: str) -> None:
    texts = page.evaluate(
        """() => Array.from(document.querySelectorAll('script[type="application/json"]'))
            .map((script) => script.textContent || '')"""
    )
    for text in texts:
        payload = parse_json_body(text)
        if payload is not None:
            walk_posts(payload, posts, allow_new=False, owner=username)


def caption_from_dom(page: Page) -> str:
    try:
        more = page.locator('article span:text-is("more"), article button:has-text("more")').first
        if more.is_visible(timeout=400):
            more.click()
            page.wait_for_timeout(300)
    except Exception:
        pass

    try:
        text = page.evaluate(
            """() => {
              const article = document.querySelector('article');
              if (!article) return '';
              const heading = article.querySelector('h1');
              return heading ? heading.innerText.trim() : '';
            }"""
        )
    except Exception:
        return ""
    text = (text or "").strip()
    return "" if is_placeholder_caption(text) else text


def collect_carousel_images(page: Page, post: Post) -> None:
    start = shortcode_from_page_url(page.url)
    for _ in range(20):
        try:
            urls = page.evaluate(
                """() => Array.from(document.querySelectorAll('article img'))
                    .map((img) => img.currentSrc || img.src)
                    .filter(Boolean)"""
            )
        except Exception:
            urls = []
        post.image_urls = merge_image_urls(post.image_urls, urls)

        nxt = page.locator('article [aria-label="Next"]').first
        try:
            if not nxt.is_visible(timeout=400):
                break
            nxt.click()
            page.wait_for_timeout(700)
        except Exception:
            break
        now = shortcode_from_page_url(page.url)
        if start and now and now != start:
            break


def capture_open_post(page: Page, post: Post, posts: dict[str, Post], username: str) -> None:
    harvest_page_json(page, posts, username)
    try:
        description = page.locator('meta[property="og:description"]').first.get_attribute(
            "content"
        )
    except Exception:
        description = None
    post.caption = prefer_caption(post.caption, caption_from_og(description, username))
    post.caption = prefer_caption(post.caption, caption_from_dom(page))
    try:
        image = page.locator('meta[property="og:image"]').first.get_attribute("content")
    except Exception:
        image = None
    if image:
        post.image_urls = merge_image_urls(post.image_urls, [image])
    collect_carousel_images(page, post)


def advance_to_next_post(page: Page, current: str) -> str | None:
    for _ in range(30):
        code = shortcode_from_page_url(page.url)
        if code and code != current:
            return code
        moved = False
        buttons = page.locator('[aria-label="Next"]')
        try:
            count = buttons.count()
        except Exception:
            count = 0
        for index in range(count - 1, -1, -1):
            button = buttons.nth(index)
            try:
                if button.is_visible(timeout=200):
                    button.click()
                    moved = True
                    break
            except Exception:
                continue
        if not moved:
            page.keyboard.press("ArrowRight")
        page.wait_for_timeout(700)
        code = shortcode_from_page_url(page.url)
        if code and code != current:
            return code
    code = shortcode_from_page_url(page.url)
    return code if code and code != current else None


def walk_profile_feed(
    page: Page,
    posts: dict[str, Post],
    username: str,
    expected: int | None,
    capture: bool,
) -> None:
    print("Walking Next through the profile feed to pick up every post...")
    try:
        page.locator('a[href*="/p/"], a[href*="/reel/"]').first.click(timeout=10_000)
        page.wait_for_timeout(1800)
    except Exception as exc:
        print(f"  could not open the first post: {exc}")
        return

    seen: set[str] = set()
    for index in range(2000):
        code = shortcode_from_page_url(page.url)
        if not code:
            break
        if code in seen:
            break
        seen.add(code)
        post = posts.setdefault(code, Post(shortcode=code))
        if capture:
            try:
                capture_open_post(page, post, posts, username)
                preview = post.caption.replace("\n", " / ")[:90] if post.caption else "(none)"
                print(
                    f"  [{len(posts)}"
                    + (f"/{expected}" if expected else "")
                    + f"] {code} slides={len(post.image_urls)} caption={preview!r}"
                )
            except Exception as exc:
                print(f"  [{code}] skipped: {exc}")
        else:
            print(f"  found {code} ({len(posts)}" + (f"/{expected}" if expected else "") + ")")

        if expected and len(posts) >= expected:
            break
        pause(page, *POST_DELAY_RANGE, f"next profile post after {code}")
        if seen and len(seen) % BREAK_EVERY_N_POSTS == 0:
            logger.info("Taking a %ss break after %s posts...", BREAK_SECONDS, len(seen))
            page.wait_for_timeout(BREAK_SECONDS * 1000)
        current = shortcode_from_page_url(page.url) or code
        if current != code:
            continue
        nxt = advance_to_next_post(page, code)
        if not nxt:
            break


def enrich_posts(page: Page, posts: dict[str, Post], username: str) -> None:
    missing = [
        post
        for post in posts.values()
        if is_placeholder_caption(post.caption) or not post.image_urls
    ]
    if not missing:
        return
    print(f"Opening {len(missing)} posts that still need captions or images...")
    for index, post in enumerate(missing, start=1):
        url = f"https://www.instagram.com/p/{post.shortcode}/"
        print(f"  [{index}/{len(missing)}] {url}")
        pause(page, *POST_DELAY_RANGE, f"opening {post.shortcode}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1800)
            dismiss_banners(page)
            capture_open_post(page, post, posts, username)
            preview = post.caption.replace("\n", " / ")[:90] if post.caption else "(none)"
            print(f"    slides={len(post.image_urls)} caption={preview!r}")
        except Exception as exc:
            logger.error("Error scraping post %s: %s", post.shortcode, exc)
            page.wait_for_timeout(30_000)
        if index % BREAK_EVERY_N_POSTS == 0:
            logger.info("Taking a %ss break after %s posts...", BREAK_SECONDS, index)
            page.wait_for_timeout(BREAK_SECONDS * 1000)


def extension_for(url: str, content_type: str) -> str:
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".mp4"):
        if ext in path:
            return ".jpg" if ext == ".jpeg" else ext
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "mp4" in content_type:
        return ".mp4"
    return ".jpg"


def download_images(page: Page, posts: dict[str, Post], out_dir: Path) -> None:
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    for post in posts.values():
        post.local_files = []
        urls = [url for url in post.image_urls if is_post_image(url)]
        post.image_urls = urls
        if not urls:
            print(f"  no images for {post.shortcode}")
            continue

        carousel = len(urls) > 1
        if carousel:
            dest_dir = images_dir / post.shortcode
            dest_dir.mkdir(parents=True, exist_ok=True)
            post.folder = str((Path("images") / post.shortcode).as_posix())
        else:
            dest_dir = images_dir
            post.folder = None

        for index, url in enumerate(urls, start=1):
            try:
                response = page.request.get(
                    url,
                    headers={"Referer": "https://www.instagram.com/"},
                    timeout=60_000,
                )
            except Exception as exc:
                print(f"  download failed {post.shortcode}[{index}]: {exc}")
                continue
            if not response.ok:
                print(f"  download failed {post.shortcode}[{index}]: HTTP {response.status}")
                continue
            ext = extension_for(url, response.headers.get("content-type", ""))
            filename = f"{index:02d}{ext}" if carousel else f"{post.shortcode}{ext}"
            path = dest_dir / filename
            path.write_bytes(response.body())
            post.local_files.append(str(path.relative_to(out_dir).as_posix()))
            print(f"  saved {path.relative_to(out_dir)}")
        pause(page, 1.0, 2.5, f"download batch {post.shortcode}")


def write_manifest(posts: dict[str, Post], out_dir: Path, username: str) -> Path:
    payload = {
        "username": username,
        "post_count": len(posts),
        "posts": [
            {
                "shortcode": post.shortcode,
                "url": f"https://www.instagram.com/p/{post.shortcode}/",
                "caption": "" if is_placeholder_caption(post.caption) else post.caption,
                "folder": post.folder,
                "files": post.local_files,
            }
            for post in posts.values()
        ],
    }
    path = out_dir / "posts.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def launch_browser(playwright: Any, launch_kwargs: dict[str, Any]) -> Any:
    attempts: list[dict[str, Any]] = [
        launch_kwargs,
        {**launch_kwargs, "channel": "chrome"},
        {**launch_kwargs, "channel": "msedge"},
    ]
    errors: list[str] = []
    for kwargs in attempts:
        channel = kwargs.get("channel", "playwright-chromium")
        try:
            browser = playwright.chromium.launch(**kwargs)
            print(f"Launched browser: {channel}")
            return browser
        except PlaywrightError as exc:
            errors.append(f"{channel}: {exc}")
    raise SystemExit(
        "Could not launch a browser. Install Playwright's Chromium with:\n"
        "  .venv/bin/playwright install chromium\n\n"
        + "\n".join(errors)
    )


def browser_context_kwargs() -> dict[str, Any]:
    return {
        "user_agent": USER_AGENT,
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-US",
        "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
    }


def save_session(args: argparse.Namespace) -> None:
    cookie_path = Path(args.cookies) if args.cookies else COOKIE_FILE
    login_user, login_password = credentials()

    with sync_playwright() as playwright:
        if args.cdp:
            browser = playwright.chromium.connect_over_cdp(args.cdp)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            if not is_logged_in(context):
                raise SystemExit(
                    "Connected to Chrome, but no Instagram session cookie was found. "
                    "Open https://www.instagram.com in that Chrome window, log in, then rerun."
                )
            save_cookies(context, cookie_path)
            print(f"Saved cookies to {cookie_path}")
            return

        browser = launch_browser(
            playwright,
            {"headless": False, "args": ["--disable-blink-features=AutomationControlled"]},
        )
        context = browser.new_context(**browser_context_kwargs())
        context.add_init_script(STEALTH_INIT)
        page = context.new_page()
        page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(2000)
        dismiss_banners(page)

        if login_user and login_password:
            try:
                fill_visible(
                    page,
                    USERNAME_SELECTORS,
                    login_user,
                    role="textbox",
                    name=re.compile(r"(mobile number|phone number|username|email)", re.I),
                )
                fill_visible(
                    page,
                    PASSWORD_SELECTORS,
                    login_password,
                    role="textbox",
                    name=re.compile(r"^password$", re.I),
                )
                page.wait_for_timeout(800)
                click_login_button(page)
                page.wait_for_timeout(3000)
            except Exception as exc:
                logger.warning("Could not auto-fill login; complete it in the browser: %s", exc)

        print("Finish login in the opened window, including any email/SMS code.")
        print("Cookies will be saved automatically once Instagram sets a session.")
        if not wait_for_manual_login(page):
            dump_login_debug(page, 0)
            browser.close()
            raise SystemExit("Timed out waiting for a logged-in Instagram session.")

        dismiss_banners(page)
        save_cookies(context, cookie_path)
        context.close()
        browser.close()
    print(f"Saved cookies to {cookie_path}")
    print("You can now run: .venv/bin/python scrape.py")


def scrape(args: argparse.Namespace) -> None:
    username = username_from_url(PROFILE_URL)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    posts: dict[str, Post] = {}

    logger.info("Profile: %s", PROFILE_URL)
    logger.info("Account: @%s", username)

    launch_kwargs: dict[str, Any] = {
        "headless": args.headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    context_kwargs: dict[str, Any] = browser_context_kwargs()
    cookie_path = Path(args.cookies) if args.cookies else COOKIE_FILE

    login_user, login_password = credentials()

    with sync_playwright() as playwright:
        browser = launch_browser(playwright, launch_kwargs)
        context = browser.new_context(**context_kwargs)
        context.add_init_script(STEALTH_INIT)
        if cookie_path.exists():
            try:
                load_cookies(context, cookie_path)
            except Exception as exc:
                logger.warning("Could not load cookies: %s", exc)
        page = context.new_page()
        stats: dict[str, Any] = {}
        listener_options: dict[str, Any] = {
            "accept_new": True,
            "owner": username,
            "stats": stats,
        }
        attach_network_listener(page, posts, listener_options)

        logged_in = False
        if cookie_path.exists():
            try:
                logged_in = session_is_valid(page)
            except Exception as exc:
                logger.warning("Saved cookies could not be verified: %s", exc)
                logged_in = False
            if not logged_in:
                logger.warning("Saved cookies are expired; logging in again")

        if not logged_in:
            if not login_user or not login_password:
                browser.close()
                raise SystemExit(
                    "Set INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD at the top of scrape.py "
                    "(or as environment variables). A logged-in session is required to scrape "
                    "more than the first 12 posts."
                )
            if not login_instagram(page, login_user, login_password, cookie_path):
                browser.close()
                raise SystemExit("Automated login failed.")
            logged_in = True

        pause(page, *QUERY_DELAY_RANGE, "opening target profile")
        page.goto(
            PROFILE_URL,
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        page.wait_for_timeout(2000)
        dismiss_banners(page)

        try:
            wait_for_grid(page)
        except PlaywrightTimeout:
            browser.close()
            raise SystemExit(
                "No posts appeared after login. Instagram may be showing a checkpoint "
                "or blocking this session."
            )

        expected = expected_post_count(page, stats)
        if expected:
            logger.info("Profile lists %s posts", expected)

        if logged_in and not cookie_path.exists():
            save_cookies(context, cookie_path)

        scroll_grid(page, posts, args.max_scrolls, expected)
        collect_from_dom(page, posts)
        expected = expected_post_count(page, stats) or expected

        if not expected or len(posts) < expected:
            walk_profile_feed(
                page,
                posts,
                username,
                expected,
                capture=args.visit_posts,
            )
            try:
                page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(1500)
            except Exception:
                pass

        if expected and len(posts) < expected:
            reels_url = PROFILE_URL.rstrip("/") + "/reels/"
            logger.info("Checking reels tab (%s/%s)...", len(posts), expected)
            try:
                pause(page, *QUERY_DELAY_RANGE, "reels tab")
                page.goto(reels_url, wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(2000)
                dismiss_banners(page)
                page.wait_for_selector('a[href*="/reel/"], a[href*="/p/"]', timeout=20_000)
                scroll_grid(page, posts, args.max_scrolls, expected)
                collect_from_dom(page, posts)
            except Exception as exc:
                print(f"  reels tab skipped: {exc}")

        if args.visit_posts:
            enrich_posts(page, posts, username)

        if not posts:
            browser.close()
            raise SystemExit("Intercepted no posts. Try logging in, then run again.")

        if expected and len(posts) < expected:
            print(
                f"Warning: collected {len(posts)} of {expected} posts. "
                "Instagram may still be rate-limiting this session."
            )
        else:
            print(f"Collected {len(posts)}" + (f" of {expected}" if expected else "") + " posts")

        print(f"Downloading images for {len(posts)} posts...")
        download_images(page, posts, out_dir)
        manifest = write_manifest(posts, out_dir, username)
        context.close()
        browser.close()

    print(f"Wrote {manifest}")
    print(f"Images: {out_dir / 'images'}")


def main() -> None:
    try:
        args = parse_args()
        if args.save_cookies:
            save_session(args)
        else:
            scrape(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
