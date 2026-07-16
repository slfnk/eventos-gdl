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
import re
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

# Canonical venue names. Keys are lowercase fragments to match against;
# values are how the venue should appear on the cartelera. Add as you go.
VENUE_ALIASES = {
    "foro diez": "Foro Diez",
    "semillero estudios": "Foro Diez",
    "cuerda cultura": "Cuerda Cultura",
    "galería sepia": "Galería Sepia",
    "galeria sepia": "Galería Sepia",
    "ritual cultural": "Ritual Cultural",
    "hake al rey": "Hake Al Rey",
    "foro larva": "Foro Larva",
    "casa mudra": "Casa Mudra",
    "foro independencia": "Foro Independencia",
}

# This is the full instruction set the Haiku model receives for every post.
# Edit the rules here to change extraction behavior; inbox.py reuses it too.
EXTRACTION_PROMPT = """\
You are extracting event info from an Instagram post by a music venue or \
art gallery in Guadalajara, Mexico. Today's date is {today}.

This post was PUBLISHED on {posted}.

The post caption is:
<caption>
{caption}
</caption>

The attached image is the post's first image (often an event flier).

Return ONLY a JSON object, no markdown fences, no commentary:
{{"events": [...], "cancellations": [...]}}

Each object in "events" has this shape:
{{
  "title": "event or show name",
  "artists": ["performer/artist names"],
  "artist_handles": {{"Artist Name": "instagram_handle_without_@"}},
  "venue": "venue name if shown, else null",
  "date": "YYYY-MM-DD or null if unparseable",
  "end_date": "YYYY-MM-DD last day ONLY if the same event runs multiple consecutive days, else null",
  "time": "HH:MM 24h or null",
  "cover": "price like '$150mxn' or '$100mxn preventa, $150mxn taquilla' or 'entrada libre', or null",
  "type": "concert | exhibition | opening | dj | workshop | other",
  "ticket_url": "ticket purchase link from the caption (boletia, passline, eventbrite, etc), or null",
  "age_restriction": "+18 if the flier or caption restricts entry to adults, else null",
  "confidence": "high or low",
  "notes": "anything else useful, max 15 words, IN SPANISH, or null"
}}

Each object in "cancellations" (when the post announces a cancellation or
postponement) has this shape:
{{"title": "name of the cancelled/postponed event", "venue": "venue or null"}}

Rules:
- Return "events": [] for recaps, memes, merch, thank-you posts, or events
  that already happened before today.
- ONE flier can contain MANY events (monthly calendars, festival lineups,
  weekly programs). Return one object per distinct date. For recurring
  programs ("todos los martes de julio", "live jazz daily"), expand into
  individual dated events within the stated period, most imminent first,
  up to {max_events} events maximum.
- BUT: one continuous event that RUNS across consecutive days (a multi-day
  workshop or laboratorio, an exhibition run, a festival with a single
  program, "22, 23 y 24 de julio") is ONE event object — set "date" to the
  first day and "end_date" to the last day. Do NOT return one object per
  day. Only split into separate dated objects when each date has a DISTINCT
  program or lineup.
- "artist_handles": map performer names (spelled EXACTLY as in "artists")
  to their Instagram handles, without the @. Include a mapping ONLY when
  the caption explicitly tags that artist (@handle) and the match is
  unambiguous. NEVER guess or invent a handle. Omit untagged artists;
  use null if none are tagged. Do not include the venue's own handle.
- ONLY include events in the Guadalajara metro area (Guadalajara, Zapopan,
  Tlaquepaque, Tonalá). If the location is clearly another city or country,
  exclude that event. If no city is shown, assume the venue's own space in GDL.
- EXCLUDE workshops/events aimed at children or families (taller infantil,
  actividades para niños, "para toda la familia"). Workshops aimed at adults
  at galleries or cultural spaces ARE included, with type "workshop".
- If the post announces a CANCELLATION or POSTPONEMENT ("cancelado",
  "pospuesto", "se pospone", "nueva fecha"), list the affected event in
  "cancellations". If a new date is announced, ALSO include the event in
  "events" with the new date.
- Set "confidence": "low" when the date, venue, or year is ambiguous, the
  flier is hard to read, or you are guessing on any key detail. Otherwise "high".
- RECAPS ARE NOT EVENTS: posts thanking attendees, sharing photos or video
  of a night that already happened, or celebrating how an event went
  ("gracias a todos", "así se vivió", "qué gran noche", "sold out anoche")
  are recaps — return "events": [], even if the original flier is re-shown.
- Fliers usually omit the year: resolve dates relative to the PUBLISH date
  of the post ({posted}), NOT today. A post published June 20 showing
  "VIE 10 JUL" means the July 10 right after June 20. If that resolved date
  is before today ({today}), the event already happened — exclude it. Never
  roll a past flier date forward to a future year.
- Format all prices in Mexican pesos as "$NNNmxn".
- Use the venue's common short name (e.g. "Foro Diez", not
  "Foro Diez - Semillero Estudios").
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


def normalize_venue(venue: str | None) -> str | None:
    """Map venue name variants to their canonical form via VENUE_ALIASES."""
    if not venue:
        return venue
    low = venue.lower()
    for fragment, canonical in VENUE_ALIASES.items():
        if fragment in low:
            return canonical
    return venue


def _norm_title(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def apply_cancellations(events: list[dict], cancellations: list[dict]) -> tuple[list[dict], list[dict]]:
    """Remove events matching announced cancellations. Returns (kept, removed)."""
    if not cancellations:
        return events, []
    kept, removed = [], []
    for ev in events:
        ev_title = _norm_title(ev.get("title"))
        hit = False
        for c in cancellations:
            c_title = _norm_title(c.get("title"))
            if not c_title or not ev_title:
                continue
            titles_match = c_title in ev_title or ev_title in c_title
            c_venue = normalize_venue(c.get("venue"))
            venue_ok = not c_venue or c_venue == ev.get("venue")
            if titles_match and venue_ok:
                hit = True
                break
        (removed if hit else kept).append(ev)
    return kept, removed




def _tokens(s: str | None) -> set[str]:
    return set(re.findall(r"[a-záéíóúñü0-9]+", (s or "").lower()))


def is_same_event(a: dict, b: dict) -> bool:
    """Heuristic: same date + compatible venue + similar title or shared artists."""
    if (a.get("date") or None) != (b.get("date") or None):
        return False
    va, vb = a.get("venue"), b.get("venue")
    if va and vb and va != vb:
        return False
    ta, tb = _norm_title(a.get("title")), _norm_title(b.get("title"))
    if ta and tb and (ta in tb or tb in ta):
        return True
    aa = {x.lower().strip() for x in (a.get("artists") or [])}
    ab = {x.lower().strip() for x in (b.get("artists") or [])}
    if aa and ab and aa & ab:
        return True
    ka, kb = _tokens(ta), _tokens(tb)
    if ka and kb and len(ka & kb) / min(len(ka), len(kb)) >= 0.6:
        return True
    return False


def dedupe_events(events: list[dict]) -> tuple[list[dict], int]:
    """Collapse duplicate listings; earlier entry wins, gaps filled from later."""
    out: list[dict] = []
    dropped = 0
    for ev in events:
        match = next((k for k in out if is_same_event(k, ev)), None)
        if match is None:
            out.append(ev)
        else:
            dropped += 1
            for key, val in ev.items():  # fill in anything the kept copy lacks
                if match.get(key) in (None, "", []) and val not in (None, "", []):
                    match[key] = val
    return out, dropped


def _day_after(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def collapse_multiday(events: list[dict]) -> tuple[list[dict], int]:
    """Safety net for one continuous event listed once per consecutive day
    (e.g. a 3-day laboratorio expanded into 3 entries by the model).
    Same title + venue + time + source, consecutive/overlapping dates ->
    one event with date = first day, end_date = last day."""
    keyed: dict[tuple, list[dict]] = {}
    out: list[dict] = []
    for ev in events:
        if not ev.get("date"):
            out.append(ev)
            continue
        key = (_norm_title(ev.get("title")), ev.get("venue"),
               ev.get("time"), ev.get("post_url") or ev.get("account"))
        keyed.setdefault(key, []).append(ev)

    collapsed = 0

    def flush(run: list[dict]):
        nonlocal collapsed
        first = run[0]
        if len(run) > 1:
            last = max((e.get("end_date") or e["date"]) for e in run)
            if last > first["date"]:
                first["end_date"] = last
            for e in run[1:]:  # backfill anything the kept copy lacks
                for k, v in e.items():
                    if k != "end_date" and first.get(k) in (None, "", []) \
                            and v not in (None, "", []):
                        first[k] = v
            collapsed += len(run) - 1
        out.append(first)

    for evs in keyed.values():
        evs.sort(key=lambda e: e["date"])
        run = [evs[0]]
        for ev in evs[1:]:
            reach = _day_after(run[-1].get("end_date") or run[-1]["date"])
            if ev["date"] <= reach:  # consecutive or overlapping day
                run.append(ev)
            else:
                flush(run)
                run = [ev]
        flush(run)
    return out, collapsed


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


def parse_extraction(text: str) -> tuple[list[dict], list[dict]]:
    """Parse the model's response. Returns (events, cancellations)."""
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(f"  unparseable model output: {text[:120]}")
        return [], []
    events = data.get("events") or []
    cancels = data.get("cancellations") or []
    if not isinstance(events, list):
        events = []
    if not isinstance(cancels, list):
        cancels = []
    events = [e for e in events if isinstance(e, dict)][:MAX_EVENTS_PER_POST]
    cancels = [c for c in cancels if isinstance(c, dict)]
    # post-process each event
    for ev in events:
        ev["venue"] = normalize_venue(ev.get("venue"))
        ev["needs_review"] = ev.pop("confidence", "high") == "low"
        # artist_handles: keep only clean {name: handle} string pairs, strip @/URLs
        handles = {}
        raw = ev.get("artist_handles")
        if isinstance(raw, dict):
            for name, h in raw.items():
                if isinstance(name, str) and isinstance(h, str):
                    h = h.strip().rstrip("/").split("/")[-1].lstrip("@").strip()
                    if h and name.strip():
                        handles[name.strip()] = h
        ev["artist_handles"] = handles or None
        # end_date must be a real range after date, else drop it
        if ev.get("end_date") and (not ev.get("date") or str(ev["end_date"]) <= str(ev["date"])):
            ev["end_date"] = None
    return events, cancels


def extract_events(post: dict) -> tuple[list[dict], list[dict]]:
    """Send caption + first image to Claude; returns (events, cancellations)."""
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
    posted = today
    ts = post.get("timestamp")
    if ts:
        try:
            posted = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            pass
    content.append(
        {
            "type": "text",
            "text": EXTRACTION_PROMPT.format(
                today=today, posted=posted, caption=caption,
                max_events=MAX_EVENTS_PER_POST,
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
    return parse_extraction(text)


# ---------------------------------------------------------------- 3. digest


def format_digest(events: list[dict], removed: list[dict] | None = None) -> str:
    lines = ["🎶 <b>Nuevos eventos detectados</b>\n"]
    events.sort(key=lambda e: e.get("date") or "9999-99-99")
    for ev in events:
        date = ev.get("date") or "fecha por confirmar"
        if ev.get("end_date"):
            date += f" → {ev['end_date']}"
        time = f" · {ev['time']}" if ev.get("time") else ""
        cover = f" · {ev['cover']}" if ev.get("cover") else ""
        flag = "⚠️ " if ev.get("needs_review") else ""
        artists = ", ".join(ev.get("artists") or [])
        title = ev.get("title") or artists or "(sin título)"
        venue = ev.get("venue") or ev.get("account", "?")
        lines.append(f"{flag}<b>{title}</b>" + (" <i>(revisar)</i>" if ev.get("needs_review") else ""))
        if artists and artists != title:
            lines.append(artists)
        lines.append(f"📍 {venue} — {date}{time}{cover}")
        lines.append(f'<a href="{ev["post_url"]}">ver post</a>\n')
    if removed:
        lines.append("🚫 <b>Cancelados / pospuestos</b>")
        for ev in removed:
            lines.append(f"— {ev.get('title')} ({ev.get('venue') or '?'})")
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
    all_cancellations = []
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
            found, cancels = extract_events(post)
        except Exception as e:  # noqa: BLE001 — one bad post shouldn't kill the run
            print(f"  extraction error, will retry next run: {e}")
            continue  # NOT marked seen -> retried tomorrow

        seen_set.add(pid)
        if cancels:
            all_cancellations.extend(cancels)
            for c in cancels:
                print(f"  🚫 cancellation notice: {c.get('title')}")
        if found:
            for event in found:
                event["account"] = account
                event["post_url"] = post.get("url") or f"https://www.instagram.com/p/{pid}/"
                event["found_at"] = datetime.now(timezone.utc).isoformat()
                new_events.append(event)
                flag = " (low confidence)" if event.get("needs_review") else ""
                print(f"  ✓ event: {event.get('title')} on {event.get('date')}{flag}")
        elif not cancels:
            print("  not an event")

    # merge, apply cancellations, drop past events (multi-day: keep until end_date)
    today_str = datetime.now(GDL_TZ).strftime("%Y-%m-%d")
    merged = [
        e for e in all_events + new_events
        if (e.get("end_date") or e.get("date") or "9999") >= today_str
    ]
    merged, removed = apply_cancellations(merged, all_cancellations)
    merged, dupes = dedupe_events(merged)
    if dupes:
        print(f"Collapsed {dupes} duplicate listing(s).")
    merged, spans = collapse_multiday(merged)
    if spans:
        print(f"Merged {spans} consecutive-day listing(s) into date ranges.")

    save_json(SEEN_FILE, sorted(seen_set)[-5000:])  # cap file growth
    save_json(EVENTS_FILE, merged)

    if new_events or removed:
        try:
            send_telegram(format_digest(new_events, removed))
            print(f"Sent digest: {len(new_events)} new, {len(removed)} removed.")
        except Exception as e:  # noqa: BLE001 — notification is optional
            print(f"Telegram send failed (events still saved): {e}")
    else:
        print("No new events today. Staying quiet.")


if __name__ == "__main__":
    main()
