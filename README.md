# GDL Events Digest

Watches a curated list of Guadalajara venue/gallery Instagram accounts once a
day, reads the fliers with Claude, and sends you a Telegram message when new
events appear. No server, no database — the GitHub repo *is* the app.

```
GitHub Actions (daily cron)
  └─ Apify instagram-scraper  → latest posts from accounts.txt
       └─ dedupe against seen.json (committed to repo)
            └─ Claude (haiku, vision) → event JSON from flier + caption
                 └─ events.json updated + Telegram digest sent
```

Cost: Apify ≈ free tier (~30 accounts × 6 posts × 30 days ≈ 5,400 posts/mo,
$1.50/1k after the free $5 credit) · Claude Haiku ≈ pennies · everything
else free.

## Setup (~20 minutes, once)

### 1. Create the repo
Push this folder to a **private** GitHub repo.

### 2. Apify
- Sign up at apify.com (free plan is fine)
- Copy your API token: Settings → Integrations → API tokens

### 3. Anthropic
- console.anthropic.com → API keys → create key
- Add $5 of credit; it will last months at this volume

### 4. Telegram bot (your delivery channel)
- Message **@BotFather** → `/newbot` → follow prompts → copy the bot token
- Message **@userinfobot** → it replies with your numeric chat id
- Send your new bot any message once (bots can't initiate chats)

### 5. GitHub secrets
Repo → Settings → Secrets and variables → Actions → add:

| Secret | Value |
|---|---|
| `APIFY_TOKEN` | from step 2 |
| `ANTHROPIC_API_KEY` | from step 3 |
| `TELEGRAM_BOT_TOKEN` | from step 4 |
| `TELEGRAM_CHAT_ID` | from step 4 |

### 6. Curate accounts.txt
This is the real work. Add 10–15 verified handles, one per line.
Verify each handle exists — a typo just silently returns nothing.

### 7. First run
Actions tab → "Daily event scan" → **Run workflow**. Watch the logs.
You should get a Telegram message with everything from the last 14 days.
After that it runs itself at ~10 AM GDL time daily and only messages you
when there's something new.

## Tuning

- `POSTS_PER_ACCOUNT` in `pipeline.py` — raise if venues post heavily
- `MAX_POST_AGE_DAYS` — the lookback window guard
- Failed extractions are *not* marked seen, so they retry the next day
- `events.json` accumulates upcoming events (past ones auto-expire) — this
  is your future calendar data source if you ever want a frontend

## Known limitations (accepted on purpose)

- **Stories are invisible.** Some venues announce only in stories. No cheap
  fix; if a venue is stories-only, that's a "check manually" account.
- **Carousels:** only the first image is read. Fliers are almost always
  image #1, so this is fine in practice.
- **Scraper fragility:** Instagram changes things; the Apify actor
  occasionally returns thin results for a day or two. If a run returns 0
  posts across all accounts, check the actor's Issues tab on Apify before
  debugging your own code.
- **Date ambiguity:** fliers without years are resolved to the next future
  occurrence. Cross-check anything that matters.

## Later, maybe (resist for now)

- Weekly "what's on this weekend" summary message (separate cron, reads
  events.json — no scraping needed)
- Static calendar page on GitHub Pages rendering events.json
- Public version — only after 2 months of daily personal use
