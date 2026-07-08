"""
GDL Events Digest — Instagram flier → structured events → Telegram.

Runs daily via GitHub Actions. State lives in JSON files committed to the repo.

Env vars required:
  APIFY_TOKEN         - apify.com API token
  ANTHROPIC_API_KEY   - console.anthropic.com API key
  TELEGRAM_BOT_TOKEN  - from @BotFather
  TELEGRAM_CHAT_ID    - your chat id (message @userinfobot to get it)
"""

import base64
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from apify_client import ApifyClient
from PIL import Image

# ---------------------------------------------------------------- config

ROOT = Path(__file__).parent
ACCOUNTS_FILE = ROOT / "accounts.txt"
SEEN_FILE = ROOT / "seen.json"
EVENTS_FILE = ROOT / "events.json"

POSTS_PER_ACCOUNT = 6          # latest N posts checked per account per run
MAX_POST_AGE_DAYS = 14         # ignore anything older (first-run guard)
CLAUDE_MODEL = "claude-haiku-4-5"
MAX_IMAGE_DIM = 1568           # resize cap keeps tokens + megapixels low
MAX_EVENTS_PER_POST = 20       # cap for monthly-calendar / residency fliers

GDL_TZ = timezone(timedelta(hours=-6))  # America/Mexico_City (no DST since 2022)

# This is the full instruction set the Haiku model receives for every post.
# Edit the rules here to change extraction behavior; inbox.py reuses it too.
EXTRACTION_PROMPT = """\
You are extracting event info from an Instagram post by a music venue or \
art gallery in Guadalajara, Mexico. Today's date is {today}.

The post caption is:
<caption>
{caption}
</caption>

The attached image is the post's first image (often an event flier).

Return ONLY a JSON object, no markdown fences, no commentary:
{{"events": [ ...zero or more event objects... ]}}

Each event object has this shape:
{{
  "title": "event or show name",
  "artists": ["performer/artist names"],
  "venue": "venue name if shown, else null",
  "date": "YYYY-MM-DD or null if unparseable",
  "time": "HH:MM 24h or null",
  "cover": "price like '$150mxn' or '$100mxn preventa, $150mxn taquilla' or 'entrada libre', or null",
  "type": "concert | exhibition | opening | dj | other",
  "notes": "anything else useful, max 15 words, IN SPANISH, or null"
}}

Rules:
- Return "events": [] for recaps, memes, merch, thank-you posts, or events
  that already happened before today.
- ONE flier can contain MANY events (monthly calendars, festival lineups,
  weekly programs). Return one object per distinct date. For recurring
  programs ("todos los martes de julio", "live jazz daily"), expand into
  individual dated events within the stated period, most imminent first,
  up to {max_events} events maximum.
- ONLY include events in the Guadalajara metro area (Guadalajara, Zapopan,
  Tlaquepaque, Tonalá). If the location is clearly another city or country,
  exclude that event. If no city is shown, assume the venue's own space in GDL.
- Fliers usually omit the year: resolve dates to the NEXT occurrence on or
  after today. "VIE 10 JUL" with today={today} means the upcoming July 10.
- Format all prices in Mexican pesos as "$NNNmxn".
- Spanish day/month abbreviations are common (VIE, SÁB, ENE, JUL...).
- Write the "notes" field in Spanish.
- If image and caption conflict, trust the image (the flier).
"""

# ---------------------------------------------------------------- helpers


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_accounts() -> list[str]:
    accounts = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("@")
        if line and not line.startswith("#"):
            accounts.append(line)
    return accounts


# ---------------------------------------------------------------- 1. scrape


def fetch_posts(accounts: list[str]) -> list[dict]:
    """Pull latest posts for each account via Apify's Instagram scraper."""
    client = ApifyClient(os.environ["APIFY_TOKEN"])
    run_input = {
        "directUrls": [f"https://www.instagram.com/{a}/" for a in accounts],
        "resultsType": "posts",
        "resultsLimit": POSTS_PER_ACCOUNT,
        "addParentData": False,
    }
    print(f"Starting Apify run for {len(accounts)} accounts...")
    run = client.actor("apify/instagram-scraper").call(run_input=run_input)
    items = list(client.dataset(run.default_dataset_id).iterate_items())
    print(f"Apify returned {len(items)} posts.")
    return items


# ---------------------------------------------------------------- 2. extract


def download_image_b64(url: str) -> tuple[str, str] | None:
    """Download an image, resize if huge, return (base64, media_type)."""
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        img = img.convert("RGB")
        if max(img.size) > MAX_IMAGE_DIM:
            img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.standard_b64encode(buf.getvalue()).decode(), "image/jpeg"
    except Exception as e:  # noqa: BLE001 — never let one image kill the run
        print(f"  image download failed: {e}")
        return None


def parse_events_json(text: str) -> list[dict]:
    """Parse the model's response into a list of event dicts."""
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(f"  unparseable model output: {text[:120]}")
        return []
    events = data.get("events") or []
    if not isinstance(events, list):
        return []
    return [e for e in events if isinstance(e, dict)][:MAX_EVENTS_PER_POST]


def extract_events(post: dict) -> list[dict]:
    """Send caption + first image to Claude; get zero or more events back."""
    caption = (post.get("caption") or "")[:2000]
    image_url = post.get("displayUrl") or (post.get("images") or [None])[0]

    content = []
    if image_url:
        img = download_image_b64(image_url)
        if img:
            b64, media_type = img
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                }
            )
    today = datetime.now(GDL_TZ).strftime("%Y-%m-%d (%A)")
    content.append(
        {
            "type": "text",
            "text": EXTRACTION_PROMPT.format(
                today=today, caption=caption, max_events=MAX_EVENTS_PER_POST
            ),
        }
    )

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 2500,  # room for multi-event fliers
            "messages": [{"role": "user", "content": content}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    text = "".join(
        block.get("text", "")
        for block in resp.json()["content"]
        if block.get("type") == "text"
    )
    return parse_events_json(text)


# ---------------------------------------------------------------- 3. digest


def format_digest(events: list[dict]) -> str:
    lines = ["🎶 <b>Nuevos eventos detectados</b>\n"]
    # sort: dated events chronologically, undated last
    events.sort(key=lambda e: e.get("date") or "9999-99-99")
    for ev in events:
        date = ev.get("date") or "fecha por confirmar"
        time = f" · {ev['time']}" if ev.get("time") else ""
        cover = f" · {ev['cover']}" if ev.get("cover") else ""
        artists = ", ".join(ev.get("artists") or [])
        title = ev.get("title") or artists or "(sin título)"
        venue = ev.get("venue") or ev.get("account", "?")
        lines.append(f"<b>{title}</b>")
        if artists and artists != title:
            lines.append(artists)
        lines.append(f"📍 {venue} — {date}{time}{cover}")
        lines.append(f'<a href="{ev["post_url"]}">ver post</a>\n')
    return "\n".join(lines)


def send_telegram(text: str):
    resp = requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        json={
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": text[:4000],  # Telegram hard limit is 4096
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------- main


def main():
    accounts = load_accounts()
    if not accounts:
        sys.exit("accounts.txt is empty — add some Instagram handles first.")

    seen: list[str] = load_json(SEEN_FILE, [])
    seen_set = set(seen)
    all_events: list[dict] = load_json(EVENTS_FILE, [])

    posts = fetch_posts(accounts)
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_POST_AGE_DAYS)

    new_events = []
    for post in posts:
        pid = post.get("shortCode") or post.get("id")
        if not pid or pid in seen_set:
            continue
        ts = post.get("timestamp")
        if ts:
            posted = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if posted < cutoff:
                seen_set.add(pid)
                continue

        account = post.get("ownerUsername", "?")
        print(f"Processing @{account} / {pid}")
        try:
            found = extract_events(post)
        except Exception as e:  # noqa: BLE001 — one bad post shouldn't kill the run
            print(f"  extraction error, will retry next run: {e}")
            continue  # NOT marked seen -> retried tomorrow

        seen_set.add(pid)
        if found:
            for event in found:
                event["account"] = account
                event["post_url"] = post.get("url") or f"https://www.instagram.com/p/{pid}/"
                event["found_at"] = datetime.now(timezone.utc).isoformat()
                new_events.append(event)
                print(f"  ✓ event: {event.get('title')} on {event.get('date')}")
        else:
            print("  not an event")

    # persist state
    today_str = datetime.now(GDL_TZ).strftime("%Y-%m-%d")
    all_events = [
        e for e in all_events + new_events
        if (e.get("date") or "9999") >= today_str  # drop past events
    ]
    save_json(SEEN_FILE, sorted(seen_set)[-5000:])  # cap file growth
    save_json(EVENTS_FILE, all_events)

   if new_events:
        try:
            send_telegram(format_digest(new_events))
            print(f"Sent digest with {len(new_events)} new events.")
        except Exception as e:  # noqa: BLE001 — notification is optional, data is not
            print(f"Telegram send failed (events still saved): {e}")
    else:
        print("No new events today. Staying quiet.")


if __name__ == "__main__":
    main()
