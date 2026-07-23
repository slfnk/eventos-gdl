# Cartelera GDL

An automated cultural-events calendar that reads event fliers straight off
Instagram and publishes them as a clean, fast website — no server, no
database, no app to install. It watches a hand-picked list of venue and
gallery accounts, uses a vision model to pull the who / what / where / when
out of each flier, and renders the result as a public page.

Built for the independent music-and-gallery scene in Guadalajara, México. It's
designed to be **forked and re-pointed at any city** — the code makes no
assumptions about GDL beyond a timezone and a list of accounts you control.

**Live reference deployment:** https://slfnk.github.io/eventos-gdl/

---

## The idea

Instagram is where the scene actually lives, and that's the problem. Event
info is scattered across dozens of accounts, buried in stories that vanish in
24 hours, and gated behind an algorithm that decides what you see. If you're
not following the right forty accounts and checking them daily, you miss
things.

This is a small machine that does that checking for you. It reads the fliers,
understands them, and puts everything upcoming in one place — in chronological
order, with no login, no ads, and no algorithm.

## How it works

The GitHub repository *is* the whole application. There is nothing else to
host. State is kept in JSON files committed back to the repo by a scheduled
job.

```
  ┌─ SCHEDULED SCRAPE (every other day, GitHub Actions) ──────────────┐
  │                                                                    │
  │  accounts.txt ──► Apify instagram-scraper ──► latest posts         │
  │                        │                                           │
  │                        ▼                                           │
  │              dedupe against seen.json                              │
  │                        │                                           │
  │                        ▼                                           │
  │       Claude (Haiku, vision) reads flier + caption                 │
  │                        │                                           │
  │                        ▼                                           │
  │      events.json updated  +  optional Telegram digest              │
  └────────────────────────────────────────────────────────────────────┘

  ┌─ MANUAL FLIER UPLOAD (optional) ──────────────────────────────────┐
  │  drop a photo in inbox/  ──►  inbox.py reads it  ──►  events.json  │
  └────────────────────────────────────────────────────────────────────┘

  ┌─ THE WEBSITE ─────────────────────────────────────────────────────┐
  │  index.html (GitHub Pages)  ──reads──►  events.json + accounts.txt │
  │  Static single file. Fetches the JSON at load. No build step.      │
  └────────────────────────────────────────────────────────────────────┘
```

Two ways events get in: the scheduled scrape, and street fliers you photograph
and drop into the `inbox/` folder. Both feed the same `events.json`, which the
website reads directly.

## What you'll need

Four accounts. Three are effectively free at this scale; the fourth (Telegram)
is optional.

| Service | Why | Cost at small scale |
|---|---|---|
| **GitHub** | Hosts the repo, runs the scheduled job, serves the site | Free |
| **Apify** | Scrapes the Instagram posts | Free monthly credit covers a small deployment; check current pricing |
| **Anthropic** | Reads the flier images (Claude Haiku, vision) | Pennies per run; a few dollars of credit lasts months |
| **Telegram** *(optional)* | Sends you a "new events found" message per run | Free |

A concrete sense of scale: the reference deployment watches ~30 accounts,
checks the 5 latest posts each, runs every other day, and comfortably fits in
or near the free tiers. Cost scales with `accounts × posts × frequency` — see
[Cost control](#cost-control). Pricing on Apify and Anthropic changes; confirm
current rates before assuming a number.

---

## Deploy your own

Budget about 30 minutes the first time. You don't need to run anything locally —
everything happens on GitHub.

### 1. Fork the repository

Fork this repo into your own account. Make the fork **public** — GitHub Pages
serves public repos for free, and the site fetching `events.json` needs the
files to be publicly readable.

### 2. Turn on GitHub Pages

Repo → **Settings → Pages** → *Build and deployment* → Source: **Deploy from a
branch** → Branch: `main`, folder `/ (root)` → Save.

After the first deploy your site is at `https://YOUR-USERNAME.github.io/YOUR-REPO/`.
(A custom domain is covered [below](#custom-domain).)

### 3. Point the front-end at your fork

Open `index.html` and edit the config block near the top of the `<script>`:

```js
const GH_OWNER  = "YOUR-USERNAME";   // your GitHub username
const GH_REPO   = "YOUR-REPO";       // your repo name
const GH_BRANCH = "main";
```

These are used by the in-page editor (below). Getting them wrong means the
"save" button silently fails, so double-check them.

### 4. Get your API keys

**Apify** — sign up at [apify.com](https://apify.com). Settings →
Integrations → API tokens → copy the token.

**Anthropic** — [console.anthropic.com](https://console.anthropic.com) → API
keys → create a key. Add a small amount of credit ($5usd lasts a long time at
this volume).

**Telegram** *(optional — skip if you only want the website)*:
- Message **@BotFather**, send `/newbot`, follow the prompts, copy the bot token.
- Message **@userinfobot**; it replies with your numeric chat id.
- Send your new bot any message once — bots can't start a conversation with you.

### 5. Add the secrets

Repo → **Settings → Secrets and variables → Actions** → New repository secret,
one per row:

| Secret name | Value |
|---|---|
| `APIFY_TOKEN` | from step 4 |
| `ANTHROPIC_API_KEY` | from step 4 |
| `TELEGRAM_BOT_TOKEN` | from step 4 *(leave unset to skip Telegram)* |
| `TELEGRAM_CHAT_ID` | from step 4 *(leave unset to skip Telegram)* |

If you skip the Telegram secrets, the scrape still runs and the website still
updates — you just won't get the notification message.

### 6. Set your city

Two files decide "which city":

- **`accounts.txt`** — replace the handles with venues and galleries in *your*
  city. This is the single most important thing you'll do. See
  [Curating accounts.txt](#curating-accountstxt).
- **`pipeline.py`** — the timezone is hard-coded near the top:

  ```py
  GDL_TZ = timezone(timedelta(hours=-6))  # America/Mexico_City
  ```

  Change the offset to your city's. This matters because flier dates ("VIE 10
  JUL", no year) are resolved relative to "today" in local time.

- **`.github/workflows/daily.yml`** — the schedule is `cron: "0 16 */2 * *"`
  (16:00 UTC, every other day ≈ 10 AM in GDL). GitHub cron is always UTC;
  adjust the hour for your timezone if you care when it runs.

Optionally edit `index.html`'s title, subheading, and the "¿qué es esto?"
explainer text to describe your city's version.

### 7. First run

Repo → **Actions** tab → **Daily event scan** → **Run workflow**. Watch the
logs. On the first run it looks back 14 days, so you should see a batch of
events land in `events.json` and (if you set up Telegram) a digest message.

After that it runs itself on the schedule and only messages you when it finds
something new. Your site updates automatically each time the job commits.

### Custom domain

Keep it on GitHub Pages — for a site that changes every couple of days there's
no reason to move. Buy a domain anywhere (Cloudflare, Namecheap, Porkbun),
then:

1. Add a file named `CNAME` at the repo root containing just your domain, e.g.
   `carteleragdl.com`.
2. At your DNS provider, point the apex at GitHub's Pages IPs
   (`185.199.108.153`, `.109.153`, `.110.153`, `.111.153`) and add a `CNAME`
   record for `www` → `YOUR-USERNAME.github.io`.
3. Repo → Settings → Pages → enter the domain and enable **Enforce HTTPS**
   (GitHub issues the certificate for you).

Total ongoing cost is just the domain (~$10–15/year). Cloudflare Pages or
Netlify are fine alternatives if you want a faster global CDN, but they're not
necessary here.

---

## Curating accounts.txt

This file is the product. The quality of your cartelera is exactly the quality
of this list. Format:

```
# Comment lines start with a hash and are ignored.

# --- música ---          <- a section label; appears as a header on the site
@venue_one
@venue_two

# --- galerías ---        <- next section
@gallery_one
```

Rules of the format:
- One Instagram handle per line; the `@` is optional.
- Lines wrapped in `# --- label ---` are **section headers**. They group the
  handles beneath them and show up as labels in the site header, in file order.
- Plain comment lines (`# note`) are ignored.

Practical advice:
- **Verify every handle exists.** A typo just silently returns nothing — no
  error, the account is simply skipped.
- Start with 10–15 solid accounts and grow from there. More accounts = more
  scraping cost and more noise to review.
- Favour accounts that post actual flier images. The extractor reads images; a
  venue that only posts text or announces exclusively in stories won't work
  well (see [Limitations](#known-limitations)).

## Adding street fliers (the inbox)

For fliers you photograph on the street that never hit Instagram:

1. Put the image (`.jpg`, `.png`, `.webp`) into the `inbox/` folder — via the
   GitHub web UI (Add file → Upload files) or however you like.
2. Committing it triggers the **Process flier inbox** workflow.
3. `inbox.py` reads the photo with the same extractor, adds any events to
   `events.json`, and deletes the photo from `inbox/` afterward.
4. Unreadable photos are reported (via Telegram if configured) so you can add
   them by hand.

Street-flier events are tagged with the source `flier callejero` instead of an
Instagram handle.

## The in-page editor

The website has a lightweight admin mode for fixing or adding events by hand —
useful when the model misreads a date or you want to feature something.

- Click **editar** in the footer to reveal edit / delete controls and an
  "agregar evento" bar.
- Saving prompts once for a GitHub **fine-grained personal access token**
  scoped to *only this repo* with **Contents: read and write**. It's stored in
  your browser's local storage and used to commit changes straight to
  `events.json`.
- This is why `GH_OWNER` / `GH_REPO` in `index.html` must match your fork.

Because the token lives only in your browser, treat the editor as a personal
tool — don't hand your token to anyone.

## Tuning

Most knobs live at the top of `pipeline.py`:

| Setting | What it does |
|---|---|
| `POSTS_PER_ACCOUNT` | How many recent posts to check per account per run (default 5). Raise for heavy-posting venues; it directly affects scraping cost. |
| `MAX_POST_AGE_DAYS` | Ignore posts older than this (default 14). Mainly a first-run guard. |
| `MAX_EVENTS_PER_POST` | Cap for monthly-calendar / festival fliers that contain many dates (default 20). |
| `CLAUDE_MODEL` | The vision model used for extraction (default Claude Haiku — cheap and good enough for flier reading). |
| `VENUE_ALIASES` | Maps messy venue-name variants to one canonical name. Add entries as you notice duplicates. |
| `EXTRACTION_PROMPT` | The full instruction set the model gets for every flier. Edit here to change what counts as an event, how dates resolve, language of notes, etc. `inbox.py` reuses it. |

Scheduling lives in `.github/workflows/daily.yml` (`cron`). Venue addresses and
map pins for the "mapa" link live in `venues.json`, keyed by canonical venue
name.

## Themes & front-end

`index.html` is a single self-contained file — HTML, CSS, and JS in one place,
no build step. It ships with three themes (a clean default, a green variant,
and a high-contrast "chaotic" mode) that cycle from the toggle in the header.
Palette lives in CSS custom properties at the top of the `<style>` block; each
theme just overrides those variables.

## Known limitations

These are understood and accepted, not bugs to file:

- **Stories are invisible.** Venues that announce only in 24-hour stories can't
  be scraped. Treat those as "check manually" accounts.
- **Carousels:** only the first image of a multi-image post is read. Flier is
  almost always image #1, so this is fine in practice.
- **Scraper fragility.** Instagram changes constantly; the Apify actor
  occasionally returns thin results for a day or two. If a run returns 0 posts
  across *all* accounts, check the actor's status on Apify before debugging
  your own setup. The pipeline degrades gracefully — a failed scrape still runs
  maintenance (expiry, dedupe, cancellations) and commits.
- **Date ambiguity.** Fliers usually omit the year; dates are resolved to the
  next future occurrence relative to the post's publish date. Anything the
  model is unsure about is flagged for review. Cross-check anything that
  matters.
- **Collaborator attribution.** Instagram "collab" posts can be returned by the
  scraper under a collaborator's handle rather than the venue's. The website
  re-attributes these to the venue's own account when it can infer it, but the
  match isn't perfect. The clean fix is upstream in how `pipeline.py` records
  the account.

## Cost control

The defaults are tuned to stay cheap:

- **Every-other-day cadence** halves scraping volume versus daily with little
  practical loss — flier posts don't expire that fast.
- **`POSTS_PER_ACCOUNT = 5`** keeps each run small.
- **Claude Haiku** is inexpensive per image.
- If your Anthropic or Apify credit runs out mid-month, the job simply stops
  producing new events until it's topped up — nothing breaks, the existing site
  stays up. The scrape step fails, the pipeline runs maintenance only, and
  commits normally.

To go cheaper still: fewer accounts, fewer `POSTS_PER_ACCOUNT`, or a less
frequent cron. To go richer: raise those, or add a second weekly "what's on
this weekend" summary job that just reads `events.json` (no scraping needed).

## Repository layout

```
index.html                     the website (single file: HTML + CSS + JS)
pipeline.py                    scheduled scrape → extract → events.json
inbox.py                       manual flier-photo processor
accounts.txt                   YOUR curated list of accounts (the product)
events.json                    upcoming events (state; auto-committed)
seen.json                      post IDs already processed (dedupe state)
venues.json                    venue addresses + map pins
requirements.txt               Python deps (apify-client, requests, Pillow)
inbox/                         drop flier photos here to process them
.github/workflows/daily.yml    the every-other-day scrape job
.github/workflows/inbox.yml    the inbox-photo job
```

## License

This project is meant to be forked and relaunched. It is released under the
**MIT License** — see the `LICENSE` file: use it, change it, run it for your
own city, commercially or not. Attribution is appreciated but not required.

*(If you're the maintainer setting this up: confirm MIT is the license you want
and put your name in the `LICENSE` file's copyright line.)*
