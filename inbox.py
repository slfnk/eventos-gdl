"""
Inbox processor — turn flier photos dropped in inbox/ into events.

Triggered by the inbox.yml workflow whenever an image lands in inbox/.
Reuses the extraction prompt and helpers from pipeline.py.

Env vars required: ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import base64
import io
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image

from pipeline import (
    CLAUDE_MODEL,
    EVENTS_FILE,
    EXTRACTION_PROMPT,
    GDL_TZ,
    MAX_EVENTS_PER_POST,
    MAX_IMAGE_DIM,
    apply_cancellations,
    collapse_multiday,
    dedupe_events,
    load_json,
    parse_extraction,
    save_json,
    send_telegram,
)

INBOX = Path(__file__).parent / "inbox"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def image_to_b64(path: Path) -> tuple[str, str]:
    img = Image.open(path).convert("RGB")
    if max(img.size) > MAX_IMAGE_DIM:
        img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode(), "image/jpeg"


def extract_from_photo(path: Path) -> tuple[list[dict], list[dict]]:
    b64, media_type = image_to_b64(path)
    today = datetime.now(GDL_TZ).strftime("%Y-%m-%d (%A)")
    prompt = EXTRACTION_PROMPT.format(
        today=today,
        posted=today,  # street flier: photographed just now
        caption="(sin caption — foto de un flier tomada en la calle)",
        max_events=MAX_EVENTS_PER_POST,
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
            "max_tokens": 2500,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        },
        timeout=120,
    )
    resp.raise_for_status()
    text = "".join(
        b.get("text", "") for b in resp.json()["content"] if b.get("type") == "text"
    )
    return parse_extraction(text)


def main():
    photos = sorted(
        p for p in INBOX.iterdir()
        if p.suffix.lower() in IMAGE_EXTS
    ) if INBOX.exists() else []

    if not photos:
        print("Inbox empty, nothing to do.")
        return

    events = load_json(EVENTS_FILE, [])
    added, failed = [], []
    all_cancellations = []

    for photo in photos:
        print(f"Processing {photo.name}")
        try:
            found, cancels = extract_from_photo(photo)
        except Exception as e:  # noqa: BLE001
            print(f"  extraction error: {e}")
            failed.append(photo.name)
            continue  # photo stays in inbox for a retry on next push

        if cancels:
            all_cancellations.extend(cancels)
        if found:
            for event in found:
                event["account"] = "flier callejero"
                event["found_at"] = datetime.now(timezone.utc).isoformat()
                events.append(event)
                added.append(event)
                flag = " (revisar)" if event.get("needs_review") else ""
                print(f"  ✓ {event.get('title')} — {event.get('date')}{flag}")
        elif not cancels:
            failed.append(photo.name)
            print("  could not read an event from this photo")
        photo.unlink()  # processed (or unreadable) — remove either way

    events, removed = apply_cancellations(events, all_cancellations)
    events, dupes = dedupe_events(events)
    if dupes:
        print(f"Collapsed {dupes} duplicate listing(s).")
    events, spans = collapse_multiday(events)
    if spans:
        print(f"Merged {spans} consecutive-day listing(s) into date ranges.")
    save_json(EVENTS_FILE, events)

    lines = []
    if added:
        lines.append("📸 <b>Fliers procesados</b>\n")
        for ev in added:
            date = ev.get("date") or "fecha por confirmar"
            venue = ev.get("venue") or "?"
            flag = " ⚠️ <i>(revisar)</i>" if ev.get("needs_review") else ""
            lines.append(f"✓ <b>{ev.get('title')}</b> — {venue}, {date}{flag}")
    if removed:
        lines.append("\n🚫 <b>Cancelados</b>")
        for ev in removed:
            lines.append(f"— {ev.get('title')}")
    if failed:
        lines.append(
            f"\n⚠️ No pude leer: {', '.join(failed)}. "
            "Agrégalo a mano en la cartelera."
        )
    if lines:
        try:
            send_telegram("\n".join(lines))
        except Exception as e:  # noqa: BLE001 — notification is optional
            print(f"Telegram send failed (events still saved): {e}")


if __name__ == "__main__":
    main()
