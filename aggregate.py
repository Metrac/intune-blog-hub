#!/usr/bin/env python3
"""Intune blog aggregator.

Reads RSS/Atom feeds listed in feeds.json, normalizes and de-duplicates the
entries, merges them into a persistent store (data/articles.json) and renders a
single self-contained dashboard (docs/index.html).

Run:  python aggregate.py
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from readability import Document

ROOT = Path(__file__).resolve().parent
FEEDS_FILE = ROOT / "feeds.json"
DATA_FILE = ROOT / "data" / "articles.json"
DOCS_DIR = ROOT / "docs"
OUTPUT_HTML = DOCS_DIR / "index.html"
POSTS_DIR = DOCS_DIR / "posts"
ASSETS_DIR = POSTS_DIR / "assets"

# Retention limits to keep the store and page a reasonable size.
MAX_AGE_DAYS = 365
MAX_ITEMS = 1000
SUMMARY_CHARS = 320
# An article counts as "new" (NEW badge) if first seen within this window.
NEW_WINDOW_HOURS = 36

# Offline scraping settings.
SCRAPE_TIMEOUT = 20            # seconds per HTTP request
MAX_IMAGES_PER_POST = 40       # cap images downloaded per article
MAX_IMAGE_BYTES = 5_000_000    # skip images larger than ~5 MB
MAX_SCRAPES_PER_RUN = 60       # bound CI runtime; new + retried posts share this budget
REQUEST_PAUSE = 1.0            # politeness delay between article fetches
USER_AGENT = "IntuneBlogHub/1.0 (offline archive; +https://github.com/)"

_IMG_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(raw: str) -> str:
    """Strip HTML tags/entities and collapse whitespace."""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def snippet(raw: str, limit: int = SUMMARY_CHARS) -> str:
    text = clean_text(raw)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def parse_date(entry) -> str | None:
    """Return an ISO-8601 UTC timestamp for the entry, or None."""
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            try:
                return datetime(*tm[:6], tzinfo=timezone.utc).isoformat()
            except (ValueError, TypeError):
                continue
    return None


def entry_id(entry, link: str) -> str:
    return entry.get("id") or entry.get("guid") or link


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  ! could not read {path.name}: {exc}", file=sys.stderr)
    return default


def fetch_feed(source: dict) -> tuple[list[dict], str | None]:
    """Return (articles, error). error is None on success."""
    name = source["name"]
    parsed = feedparser.parse(source["url"])

    # feedparser sets bozo for malformed feeds; a fatal error has no entries.
    if parsed.bozo and not parsed.entries:
        reason = getattr(parsed, "bozo_exception", "unknown error")
        return [], f"{type(reason).__name__ if isinstance(reason, Exception) else 'error'}: {reason}"

    status = parsed.get("status")
    if status and status >= 400:
        return [], f"HTTP {status}"

    articles = []
    for entry in parsed.entries:
        link = entry.get("link", "")
        if not link:
            continue
        summary_raw = entry.get("summary") or (
            entry.get("content", [{}])[0].get("value", "")
            if entry.get("content")
            else ""
        )
        articles.append(
            {
                "id": entry_id(entry, link),
                "title": clean_text(entry.get("title", "(untitled)")),
                "link": link,
                "source": name,
                "source_category": source.get("category", "Community"),
                "published": parse_date(entry),
                "summary": snippet(summary_raw),
            }
        )
    return articles, None


def merge(existing: list[dict], fetched: list[dict], now_iso: str) -> tuple[list[dict], int]:
    """Merge fetched articles into existing store keyed by id. Returns (store, new_count)."""
    by_id = {a["id"]: a for a in existing}
    new_count = 0
    for art in fetched:
        if art["id"] in by_id:
            # Refresh mutable fields but carry over state we own: the original
            # first_seen timestamp and any offline-scrape results.
            kept = by_id[art["id"]]
            art["first_seen"] = kept.get("first_seen", now_iso)
            for field in ("offline_path", "scraped_at", "image_count", "scrape_error"):
                if field in kept:
                    art[field] = kept[field]
            by_id[art["id"]] = art
        else:
            art["first_seen"] = now_iso
            by_id[art["id"]] = art
            new_count += 1
    return list(by_id.values()), new_count


def sort_key(article: dict) -> str:
    return article.get("published") or article.get("first_seen") or ""


def prune(articles: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    kept = []
    for art in articles:
        stamp = art.get("published") or art.get("first_seen")
        try:
            when = datetime.fromisoformat(stamp) if stamp else None
        except ValueError:
            when = None
        if when is None or when >= cutoff:
            kept.append(art)
    kept.sort(key=sort_key, reverse=True)
    return kept[:MAX_ITEMS]


def render_html(articles: list[dict], generated_iso: str) -> str:
    sources = sorted({a["source"] for a in articles})
    new_cutoff = datetime.now(timezone.utc) - timedelta(hours=NEW_WINDOW_HOURS)
    enriched = []
    for art in articles:
        first_seen = art.get("first_seen")
        try:
            is_new = bool(first_seen) and datetime.fromisoformat(first_seen) >= new_cutoff
        except ValueError:
            is_new = False
        enriched.append(
            {
                "title": art.get("title", ""),
                "link": art.get("link", ""),
                "source": art.get("source", ""),
                "source_category": art.get("source_category", "Community"),
                "published": art.get("published"),
                "summary": art.get("summary", ""),
                "offline_path": art.get("offline_path") or None,
                "is_new": is_new,
            }
        )
    payload = json.dumps(
        {"articles": enriched, "sources": sources, "generated": generated_iso},
        ensure_ascii=False,
    )
    # Guard against the data blob accidentally closing the script tag.
    payload = payload.replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__DATA__", payload).replace(
        "__GENERATED__", html.escape(generated_iso)
    )


def post_hash(article: dict) -> str:
    """Stable, filesystem-safe identifier derived from the article id."""
    return hashlib.sha1(article["id"].encode("utf-8")).hexdigest()[:16]


def _image_ext(url: str, content_type: str | None) -> str:
    if content_type:
        ext = _IMG_EXT_BY_TYPE.get(content_type.split(";")[0].strip().lower())
        if ext:
            return ext
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".img"


def download_image(url: str, dest_dir: Path, index: int, session: requests.Session) -> str | None:
    """Download one image into dest_dir; return its local filename or None on failure."""
    try:
        resp = session.get(url, timeout=SCRAPE_TIMEOUT, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if content_type and not content_type.lower().startswith("image/"):
            return None
        length = resp.headers.get("Content-Length")
        if length and int(length) > MAX_IMAGE_BYTES:
            return None
        data = b""
        for chunk in resp.iter_content(chunk_size=65536):
            data += chunk
            if len(data) > MAX_IMAGE_BYTES:
                return None
        if not data:
            return None
        filename = f"img_{index}{_image_ext(url, content_type)}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / filename).write_bytes(data)
        return filename
    except (requests.RequestException, ValueError, OSError):
        return None


def scrape_article(article: dict, session: requests.Session) -> dict:
    """Fetch and extract a reader-view copy of the article with local images.

    Always returns a status dict (never raises): on success offline_path,
    scraped_at, image_count; on failure scrape_error (and offline_path=None).
    """
    h = post_hash(article)
    link = article["link"]
    scraped_at = datetime.now(timezone.utc).isoformat()

    def fail(reason: str) -> dict:
        return {"offline_path": None, "scrape_error": reason[:200], "scraped_at": scraped_at}

    try:
        resp = session.get(link, timeout=SCRAPE_TIMEOUT)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if ctype and "html" not in ctype.lower():
            return fail(f"not html ({ctype.split(';')[0]})")

        # readability isolates the main article body as clean HTML.
        content_html = Document(resp.text).summary(html_partial=True)
        if not content_html or len(content_html) < 50:
            return fail("no main content")

        soup = BeautifulSoup(content_html, "lxml")
        assets_dir = ASSETS_DIR / h
        image_count = 0
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            # Drop responsive/lazy attributes that would otherwise pull from the web.
            for attr in ("srcset", "data-srcset", "data-src", "data-lazy-src", "loading", "sizes"):
                if img.has_attr(attr):
                    del img[attr]
            if not src or image_count >= MAX_IMAGES_PER_POST:
                img.decompose()
                continue
            abs_url = urljoin(link, src)
            if not abs_url.lower().startswith(("http://", "https://")):
                img.decompose()
                continue
            local = download_image(abs_url, assets_dir, image_count, session)
            if local:
                img["src"] = f"assets/{h}/{local}"
                image_count += 1
            else:
                img.decompose()

        # lxml wraps fragments in <html><body>; emit only the inner content.
        container = soup.body or soup
        body_html = container.decode_contents()
        page = render_post(article, body_html)
        POSTS_DIR.mkdir(parents=True, exist_ok=True)
        (POSTS_DIR / f"{h}.html").write_text(page, encoding="utf-8")
    except requests.RequestException as exc:
        return fail(f"fetch failed: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - record and skip, never abort the run
        return fail(f"{type(exc).__name__}: {exc}")

    return {
        "offline_path": f"posts/{h}.html",
        "scraped_at": scraped_at,
        "image_count": image_count,
        "scrape_error": None,
    }


def render_post(article: dict, body_html: str) -> str:
    cat = (article.get("source_category") or "Community").lower()
    badge_class = "official" if cat == "official" else "community"
    date = ""
    iso = article.get("published")
    if iso:
        try:
            date = datetime.fromisoformat(iso).strftime("%B %-d, %Y")
        except ValueError:
            date = ""
    return (
        POST_TEMPLATE.replace("__TITLE__", html.escape(article.get("title", "(untitled)")))
        .replace("__SOURCE__", html.escape(article.get("source", "")))
        .replace("__BADGE__", badge_class)
        .replace("__DATE__", html.escape(date))
        .replace("__ORIGINAL__", html.escape(article.get("link", "")))
        .replace("__BODY__", body_html)
    )


def scrape_pending(store: list[dict], session: requests.Session) -> tuple[int, int, int]:
    """Scrape un-archived articles (newest first), bounded by MAX_SCRAPES_PER_RUN.

    Returns (scraped_ok, images_total, failed). Errored posts are retried on
    later runs (their scrape_error is cleared each attempt) but share the budget.
    """
    pending = [
        a for a in store
        if not a.get("offline_path")  # never successfully scraped yet
    ]
    pending.sort(key=sort_key, reverse=True)  # newest first
    budget = pending[:MAX_SCRAPES_PER_RUN]

    ok = images = failed = 0
    for art in budget:
        result = scrape_article(art, session)
        art.update(result)
        if result.get("offline_path"):
            ok += 1
            images += result.get("image_count", 0)
            print(f"  saved  {art['source']}: {art['title'][:60]}")
        else:
            failed += 1
            print(f"  miss   {art['source']}: {result.get('scrape_error')}")
        time.sleep(REQUEST_PAUSE)
    return ok, images, failed


def cleanup_orphans(store: list[dict]) -> int:
    """Delete reader pages and asset folders for articles no longer in the store."""
    keep = {post_hash(a) for a in store}
    removed = 0
    if POSTS_DIR.exists():
        for page in POSTS_DIR.glob("*.html"):
            if page.stem not in keep:
                page.unlink()
                removed += 1
    if ASSETS_DIR.exists():
        for folder in ASSETS_DIR.iterdir():
            if folder.is_dir() and folder.name not in keep:
                shutil.rmtree(folder, ignore_errors=True)
    return removed


def main() -> int:
    feeds = load_json(FEEDS_FILE, [])
    if not feeds:
        print("No feeds configured in feeds.json", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    store = load_json(DATA_FILE, [])
    print(f"Loaded {len(store)} existing articles.\n")

    total_new = 0
    failures = []
    for source in feeds:
        articles, error = fetch_feed(source)
        if error:
            failures.append((source["name"], error))
            print(f"  FAIL  {source['name']}: {error}")
            continue
        store, new_count = merge(store, articles, now_iso)
        total_new += new_count
        print(f"  ok    {source['name']}: {len(articles)} items ({new_count} new)")

    store = prune(store)

    # Remove offline copies for articles that pruning dropped from the store.
    orphans = cleanup_orphans(store)
    if orphans:
        print(f"\nCleaned up {orphans} orphaned offline page(s).")

    # Archive un-scraped articles for offline reading (bounded per run).
    print("\nScraping articles for offline access...")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    saved, images, missed = scrape_pending(store, session)
    print(f"  -> {saved} saved ({images} images), {missed} failed this run.")

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML.write_text(render_html(store, now_iso), encoding="utf-8")

    offline_total = sum(1 for a in store if a.get("offline_path"))
    print(
        f"\nDone. {len(store)} articles in store, {total_new} new this run."
        f"\n  {offline_total} available offline."
        f"\n  data -> {DATA_FILE.relative_to(ROOT)}"
        f"\n  page -> {OUTPUT_HTML.relative_to(ROOT)}"
    )
    if failures:
        print(f"\n{len(failures)} feed(s) failed:")
        for name, err in failures:
            print(f"  - {name}: {err}")
    return 0


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Intune Blog Hub</title>
<style>
  :root {
    --bg: #f4f6fb; --card: #ffffff; --text: #1b2430; --muted: #5d6b7d;
    --border: #e2e8f0; --accent: #0b6efd; --accent-soft: #e7f0ff;
    --official: #0b6efd; --community: #2fa37a; --new: #e8602c;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f141b; --card: #19212c; --text: #e6edf5; --muted: #93a1b3;
      --border: #2a3543; --accent: #4d97ff; --accent-soft: #1b2a40;
      --official: #4d97ff; --community: #43c39b; --new: #ff7a45;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  header {
    position: sticky; top: 0; z-index: 5; background: var(--bg);
    border-bottom: 1px solid var(--border); padding: 18px 20px 14px;
  }
  .wrap { max-width: 920px; margin: 0 auto; }
  h1 { margin: 0 0 2px; font-size: 22px; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 13px; }
  .controls { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
  #search {
    flex: 1 1 240px; min-width: 200px; padding: 9px 12px; font-size: 14px;
    border: 1px solid var(--border); border-radius: 9px; background: var(--card);
    color: var(--text);
  }
  #source {
    padding: 9px 12px; font-size: 14px; border: 1px solid var(--border);
    border-radius: 9px; background: var(--card); color: var(--text);
  }
  .toggle {
    display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted);
    padding: 0 4px; cursor: pointer; user-select: none;
  }
  .toggle input { accent-color: var(--accent); }
  main { max-width: 920px; margin: 18px auto 60px; padding: 0 20px; }
  .count { color: var(--muted); font-size: 13px; margin-bottom: 12px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 14px 16px; margin-bottom: 12px; transition: border-color .15s;
  }
  .card:hover { border-color: var(--accent); }
  .card .top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
  .badge {
    font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px;
    background: var(--accent-soft); color: var(--accent);
  }
  .badge.official { background: var(--accent-soft); color: var(--official); }
  .badge.new { background: var(--new); color: #fff; }
  .date { color: var(--muted); font-size: 12px; margin-left: auto; }
  .card a.title { color: var(--text); text-decoration: none; font-weight: 600; font-size: 16px; }
  .card a.title:hover { color: var(--accent); text-decoration: underline; }
  .card p { margin: 6px 0 0; color: var(--muted); font-size: 14px; }
  .card .links { margin-top: 8px; display: flex; gap: 14px; align-items: center; font-size: 12.5px; }
  .card .links a { color: var(--accent); text-decoration: none; }
  .card .links a:hover { text-decoration: underline; }
  .card .links .nooff { color: var(--muted); }
  .badge.offline { background: var(--community); color: #fff; }
  .empty { text-align: center; color: var(--muted); padding: 60px 0; }
  footer { text-align: center; color: var(--muted); font-size: 12px; padding: 30px 0 40px; }
  footer a { color: var(--accent); }
</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>Intune Blog Hub</h1>
    <div class="sub">Latest Microsoft Intune posts from official &amp; community blogs &middot; updated <span id="updated"></span></div>
    <div class="controls">
      <input id="search" type="search" placeholder="Search titles &amp; summaries…" autocomplete="off">
      <select id="source"><option value="">All sources</option></select>
      <label class="toggle"><input id="offlineOnly" type="checkbox"> Offline only</label>
    </div>
  </div>
</header>
<main>
  <div class="count" id="count"></div>
  <div id="list"></div>
  <div class="empty" id="empty" hidden>No posts match your filter.</div>
</main>
<footer>
  Generated by <a href="https://feedparser.readthedocs.io/">feedparser</a> &middot;
  edit <code>feeds.json</code> to add sources.
</footer>
<script id="data" type="application/json">__DATA__</script>
<script>
  const DATA = JSON.parse(document.getElementById("data").textContent);
  const listEl = document.getElementById("list");
  const countEl = document.getElementById("count");
  const emptyEl = document.getElementById("empty");
  const searchEl = document.getElementById("search");
  const sourceEl = document.getElementById("source");
  const offlineEl = document.getElementById("offlineOnly");

  document.getElementById("updated").textContent =
    new Date(DATA.generated).toLocaleString(undefined,
      { dateStyle: "medium", timeStyle: "short" });

  for (const s of DATA.sources) {
    const opt = document.createElement("option");
    opt.value = s; opt.textContent = s;
    sourceEl.appendChild(opt);
  }

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return "";
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function render() {
    const q = searchEl.value.trim().toLowerCase();
    const src = sourceEl.value;
    const offlineOnly = offlineEl.checked;
    const items = DATA.articles.filter(a => {
      if (src && a.source !== src) return false;
      if (offlineOnly && !a.offline_path) return false;
      if (q) {
        const hay = (a.title + " " + (a.summary || "")).toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    countEl.textContent = `${items.length} post${items.length === 1 ? "" : "s"}` +
      (src || q ? ` (of ${DATA.articles.length})` : "");
    emptyEl.hidden = items.length > 0;

    listEl.innerHTML = items.map(a => {
      const cat = (a.source_category || "Community").toLowerCase();
      const isNew = a.is_new ? '<span class="badge new">NEW</span>' : "";
      const off = a.offline_path ? '<span class="badge offline">OFFLINE</span>' : "";
      // Title opens the local offline copy when available, else the live site.
      const titleHref = a.offline_path ? escapeHtml(a.offline_path) : escapeHtml(a.link);
      const titleTarget = a.offline_path ? "" : ' target="_blank" rel="noopener"';
      const links = a.offline_path
        ? `<a href="${escapeHtml(a.link)}" target="_blank" rel="noopener">Original ↗</a>`
        : `<a href="${escapeHtml(a.link)}" target="_blank" rel="noopener">Read on site ↗</a>
           <span class="nooff">offline copy unavailable</span>`;
      return `<article class="card">
        <div class="top">
          <span class="badge ${cat === "official" ? "official" : ""}">${escapeHtml(a.source)}</span>
          ${off}
          ${isNew}
          <span class="date">${fmtDate(a.published)}</span>
        </div>
        <a class="title" href="${titleHref}"${titleTarget}>${escapeHtml(a.title)}</a>
        ${a.summary ? `<p>${escapeHtml(a.summary)}</p>` : ""}
        <div class="links">${links}</div>
      </article>`;
    }).join("");
  }

  searchEl.addEventListener("input", render);
  sourceEl.addEventListener("change", render);
  offlineEl.addEventListener("change", render);
  render();
</script>
</body>
</html>
"""


POST_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #f4f6fb; --card: #ffffff; --text: #1b2430; --muted: #5d6b7d;
    --border: #e2e8f0; --accent: #0b6efd; --accent-soft: #e7f0ff;
    --official: #0b6efd; --community: #2fa37a;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f141b; --card: #19212c; --text: #e6edf5; --muted: #93a1b3;
      --border: #2a3543; --accent: #4d97ff; --accent-soft: #1b2a40;
      --official: #4d97ff; --community: #43c39b;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  .bar {
    position: sticky; top: 0; background: var(--bg); border-bottom: 1px solid var(--border);
    padding: 12px 20px;
  }
  .bar a { color: var(--accent); text-decoration: none; font-size: 14px; }
  .bar a:hover { text-decoration: underline; }
  article { max-width: 760px; margin: 26px auto 80px; padding: 0 22px; }
  .meta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
  .badge {
    font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px;
    background: var(--accent-soft); color: var(--accent);
  }
  .badge.official { color: var(--official); }
  .badge.community { color: var(--community); }
  .date { color: var(--muted); font-size: 13px; }
  h1 { font-size: 28px; line-height: 1.25; letter-spacing: -0.01em; margin: 4px 0 14px; }
  .orig { font-size: 13px; }
  .orig a { color: var(--accent); }
  .content { margin-top: 22px; }
  .content img { max-width: 100%; height: auto; border-radius: 8px; display: block; margin: 16px 0; }
  .content pre {
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    padding: 12px 14px; overflow-x: auto; font-size: 13.5px;
  }
  .content code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .content :not(pre) > code {
    background: var(--card); border: 1px solid var(--border); border-radius: 5px; padding: 1px 5px;
  }
  .content a { color: var(--accent); }
  .content blockquote {
    margin: 16px 0; padding: 4px 16px; border-left: 3px solid var(--border); color: var(--muted);
  }
  .content table { border-collapse: collapse; max-width: 100%; overflow-x: auto; display: block; }
  .content th, .content td { border: 1px solid var(--border); padding: 6px 10px; }
  hr { border: none; border-top: 1px solid var(--border); margin: 22px 0; }
</style>
</head>
<body>
<div class="bar"><a href="../index.html">← Back to Intune Blog Hub</a></div>
<article>
  <div class="meta">
    <span class="badge __BADGE__">__SOURCE__</span>
    <span class="date">__DATE__</span>
  </div>
  <h1>__TITLE__</h1>
  <div class="orig">Saved for offline reading · <a href="__ORIGINAL__" target="_blank" rel="noopener">Open original ↗</a></div>
  <hr>
  <div class="content">__BODY__</div>
</article>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
