# Telegram Forwarder Bot

A self-hosted Telegram bot that forwards whatever you send it to a chosen topic (forum thread) inside a destination group. Supports pulling content from locked / private channels via your personal Telegram account session (Telethon).

## Features

| Feature | How |
|---|---|
| Forward text / photo / video / file / album | Send to bot -> tap a topic button |
| Auto-discover forum topics | `/refresh` enumerates via Telethon `GetForumTopics` |
| Manual topic override | `/addtopic <title> <id>` |
| Pull from a locked private channel | Send a `t.me/c/<id>/<msg_id>` link |
| Pull from a public channel | Send a `t.me/<channelname>/<msg_id>` link |
| Persistent state | SQLite (`forwarder.db`) |
| Admin whitelist | Optional `ADMIN_IDS` in `.env` |
| **Deploy to Render free tier** | Webhook mode + `StringSession` |

The bot re-uploads media from locked channels (it does NOT use Telegram's forward feature) — this works even when the source channel has "forwarding / saving restricted" enabled, because your **personal account** can still view the content and the bot re-uploads the bytes through your user session's download.

---

## Quick start (two paths)

| If you want to... | Follow... |
|---|---|
| Run it on your own machine or VPS | [Local setup](#local-setup) |
| Deploy to Render.com free tier | [Render setup](#render-setup) |

---

## Local setup

### 1. Prerequisites

- Python 3.10 or newer
- A Telegram bot token — create a bot via [@BotFather](https://t.me/BotFather), copy the token
- Telegram API credentials — get `API_ID` and `API_HASH` from <https://my.telegram.org/apps> (sign in -> "API development tools")
- A destination group that is a **forum** (topics enabled). The bot must be a member with "Send Messages" permission. Your personal Telegram account must also be a member (so Telethon can enumerate topics).

### 2. Install

```bash
cd telegram-forwarder-bot

python -m venv .venv
source .venv/bin/activate    # on Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
```

Edit `.env`:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | From @BotFather |
| `API_ID` | From my.telegram.org |
| `API_HASH` | From my.telegram.org |
| `PHONE` | Your phone (used only by `login.py`) |
| `DESTINATION_GROUP_ID` | e.g. `-1001234567890` (see below) |
| `ADMIN_IDS` | Comma-separated Telegram user IDs (optional but recommended) |

### 3. One-time Telethon login

```bash
python login.py
```

You'll be prompted for:
- A confirmation code sent to your Telegram
- (Optional) 2FA password if your account has it

This creates `user_session.session` — a saved login for **your personal Telegram account**. The bot uses it to:
- Enumerate forum topics in your destination group
- Read messages from locked/private channels you are a member of

### 4. Add the bot to the destination group

1. Add the bot to your destination group as a **member** (not admin — member is enough)
2. Make sure "Send Messages" is allowed for the bot
3. If the group is a forum, the bot will be able to post into specific topics

### 5. Run

```bash
python bot.py
```

### 6. First-time configuration

In a private chat with the bot:

```
/setgroup -1001234567890
/refresh
/topics        # confirm topics were discovered
```

If `/refresh` fails (e.g. because your account isn't a member of the group), you can add topics manually:

```
/addtopic Videos 12
/addtopic Images 34
```

### 7. Using the bot

**Direct forward** — send anything to the bot in a private chat. It will reply with inline buttons showing the available topics. Tap one to forward.

**Locked-channel forward** — paste a link to the message you want:

```
https://t.me/c/1234567890/42
https://t.me/privatechannelname/42
```

The bot fetches via your Telethon session, downloads media to a temp dir, and shows the topic picker. After you tap a topic, it re-uploads via the Bot API.

---

## Render setup

Render's free tier is ideal for a personal bot. The free plan has these caveats — read them so you know what to expect:

- **The service sleeps after 15 min of no inbound HTTP requests.** When someone sends the bot a message, Telegram hits the webhook URL, Render wakes the service (30-60 sec cold start), and Telegram retries. The first message after a long idle period will be slow; subsequent ones are instant.
- **Ephemeral filesystem** — files don't persist across restarts. That's why we use `StringSession` (stored in an env var) instead of a `.session` file.
- **512 MB RAM, 0.1 CPU** — plenty for this bot.
- **750 free instance-hours per month** — enough for ~31 days of 24/7 (with sleep, much less is actually used).

To keep the service awake 24/7, see [Keep the service awake (optional)](#keep-the-service-awake-optional) below.

### Step 1. Push the project to GitHub

The simplest path:

```bash
# On your local machine:
cd telegram-forwarder-bot
git init
git add .
git commit -m "Initial commit: Telegram Forwarder Bot"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

Make sure `.gitignore` is honored (it is by default — it excludes `.env`, `*.session`, `forwarder.db`).

### Step 2. Get your Telegram credentials ready

Before deploying, have these values on hand:

| Value | Where to get it |
|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) -> create a bot -> copy the token |
| `API_ID` | <https://my.telegram.org/apps> -> "API development tools" |
| `API_HASH` | Same place as `API_ID` |
| `SESSION_STRING` | Generate locally — see **Step 3** below |
| `DESTINATION_GROUP_ID` | Add [@RawDataBot](https://t.me/RawDataBot) to your group, copy the chat_id it prints, then remove it |
| `ADMIN_IDS` | Your Telegram user ID from [@userinfobot](https://t.me/userinfobot) |

### Step 3. Generate the SESSION_STRING locally

You need to do this **once**, on your local machine. The output string is what Render will use as the Telethon session — no `.session` file needed on the server.

```bash
# On your local machine, inside the project directory:
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: fill in API_ID, API_HASH, PHONE (other vars don't matter for this step)

python login.py --string
```

You'll be prompted for the SMS code (and 2FA password if you have one). After successful login, the script prints:

```
======================================================================
  SESSION_STRING (copy this ENTIRE line, including '1' prefix):

  1BVtsOH8Bu5T2nTRrQzQzF1kT3FvYzBEXAMPLE...long_string...

======================================================================
```

**Copy that entire string** (including the leading `1`). Save it somewhere safe — it grants full access to your Telegram account.

### Step 4. Deploy to Render

Two ways to deploy:

#### Option A. Blueprint (recommended — uses `render.yaml`)

1. Go to <https://dashboard.render.com> -> **New** -> **Blueprint**
2. Select your GitHub repo. Render reads `render.yaml` and creates the service automatically.
3. Click **Apply**. Render creates a free Web Service named `telegram-forwarder-bot`.

#### Option B. Manual

1. Go to <https://dashboard.render.com> -> **New** -> **Web Service**
2. Connect your GitHub account and select your repo
3. Configure:
   - **Name:** `telegram-forwarder-bot` (or whatever you want)
   - **Region:** closest to you
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `python bot.py`
   - **Plan:** Free
4. Click **Create Web Service**

### Step 5. Set environment variables in Render

After the service is created (the first deploy will likely FAIL — that's OK, see below):

1. In Render, open your service -> **Environment** (left sidebar)
2. Add the following environment variables:

| Key | Value | Required |
|---|---|---|
| `MODE` | `webhook` | Yes — set this FIRST |
| `BOT_TOKEN` | your bot token from @BotFather | Yes |
| `API_ID` | your API ID from my.telegram.org | Yes (for locked channels) |
| `API_HASH` | your API hash from my.telegram.org | Yes (for locked channels) |
| `SESSION_STRING` | the long string from `python login.py --string` | Yes (for locked channels) |
| `DESTINATION_GROUP_ID` | e.g. `-1001234567890` | Yes |
| `ADMIN_IDS` | e.g. `123456789` | Recommended |
| `WEBHOOK_URL` | `https://<your-service-name>.onrender.com` | Yes — see Step 6 |
| `DB_PATH` | `/tmp/forwarder.db` | Optional (defaults to this on Render) |

3. Click **Save Changes**. Render redeploys automatically.

### Step 6. Set the WEBHOOK_URL (after the first deploy)

You can only know your service's public URL **after the first deploy**. Here's the workflow:

1. **First deploy** — the service builds and starts, but the bot will log `MODE=webhook but WEBHOOK_URL is not set!` and Telegram cannot reach it yet. That's expected.
2. In Render, open your service. At the top of the page, you'll see the URL, e.g. `https://telegram-forwarder-bot-xyz.onrender.com`.
3. Copy that URL. Go to **Environment** -> add or update `WEBHOOK_URL` with this value (no trailing slash):
   ```
   https://telegram-forwarder-bot-xyz.onrender.com
   ```
4. Click **Save Changes**. Render redeploys. On startup, the bot calls `setWebhook` on Telegram, registering `https://your-service.onrender.com/<bot-token-secret>` as the webhook URL.
5. Verify in Render logs that you see:
   ```
   Webhook URL: https://telegram-forwarder-bot-xyz.onrender.com/ABC-DEF...
   Bot started: @your_bot (123456789) mode=webhook
   ```

### Step 7. Configure the bot

In a private chat with the bot (on Telegram):

```
/setgroup -1001234567890
/refresh
/topics
/status
```

`/status` should report:
- Telethon user session: connected
- Destination group: <your group id>
- Known topics: N
- Admin whitelist: 1 user(s)

### Step 8. Test it

Send the bot a photo. It should reply with inline buttons for each topic. Tap one — the photo gets forwarded to that topic in your group.

Send the bot a `t.me/c/<id>/<msg>` link from a locked channel. It should fetch the content via your Telethon session and offer the topic picker.

### Keep the service awake (optional)

Render's free tier sleeps the service after 15 min of inactivity. When asleep:
- The first message after the idle period takes 30-60 sec (Render cold start)
- Subsequent messages are instant

If you want 24/7 responsiveness without paying, set up a free [UptimeRobot](https://uptimerobot.com) monitor:

1. Sign up at <https://uptimerobot.com> (free plan: 50 monitors, 5-min checks)
2. Add a new monitor:
   - Type: HTTP(s)
   - URL: `https://your-service-name.onrender.com/` (or any path — the webhook server returns 404 on unknown paths, which UptimeRobot considers "up")
   - Interval: 5 minutes
3. Save. UptimeRobot will ping your service every 5 min, keeping it awake.

**Note:** This counts toward Render's 750 free instance-hours per month. With a 5-min ping, the service stays awake 24/7, using ~720 hours/month — under the limit. If you skip the ping, the service sleeps but you'll see a 30-60 sec delay on the first message after each 15-min idle period.

---

## Commands

| Command | Action |
|---|---|
| `/start` | Show the intro message |
| `/help` | Show the help message |
| `/setgroup <id>` | Set the destination group |
| `/refresh` | Re-fetch forum topics via Telethon |
| `/topics` | List known topics |
| `/addtopic <title> <id>` | Add a topic manually |
| `/deltopic <id>` | Remove a manually-added topic |
| `/status` | Show bot status (Telethon, group, topics, admins) |
| `/cancel` | Cancel any pending forward for this chat |

## Troubleshooting

### On Render

**The first deploy fails with "MODE=webhook requires WEBHOOK_URL"**
That's expected — you can only know the URL after the first deploy. Set `WEBHOOK_URL` in Render's Environment tab and redeploy.

**Logs show `Telethon session unavailable`**
`SESSION_STRING` is missing or invalid. Generate it locally with `python login.py --string` and paste the entire printed string into Render's `SESSION_STRING` env var. The string starts with `1` — include that.

**Logs show `MODE=webhook but WEBHOOK_URL is not set!`**
You didn't set `WEBHOOK_URL` in Render's Environment tab. Set it to `https://your-service-name.onrender.com` (no trailing slash).

**Bot doesn't respond after a long idle period**
Render free tier sleeps the service after 15 min of no inbound HTTP. The first message after sleep takes 30-60 sec. See [Keep the service awake](#keep-the-service-awake-optional) to prevent this.

**`/refresh` returns "Telethon user session not available"**
Your `SESSION_STRING` is invalid or expired. Re-run `python login.py --string` locally and update the env var in Render.

### Local

**`Telethon user session not available` after starting the bot**
Run `python login.py` first. The session file `user_session.session` must exist.

**`/refresh` returns "No topics found"**
- Confirm your **personal account** is a member of the destination group (not just the bot)
- Confirm the group is a forum (topics enabled)
- Use `/addtopic <title> <id>` to add topics manually as a fallback

**Bot can't send to the destination group**
Make sure the bot is a member of the group and "Send Messages" is enabled in its permissions.

**Locked-channel link returns "Couldn't fetch that message"**
- Your personal account must be a member of that channel
- The link must point to a real post (try opening it in Telegram — if you can't open it, neither can Telethon)
- If the channel uses a custom domain / private invite link (`t.me/+abc...`), the bot can't auto-resolve it — you need a direct post link (`t.me/c/<id>/<msg>` or `t.me/<username>/<msg>`)

**Forwarding an album — items appear as separate messages in the destination**
This is a current limitation of using `copy_message` per item. To preserve album grouping, the bot would need to extract media and re-send via `send_media_group`. For now, captions are preserved on the first item.

## File layout

```
telegram-forwarder-bot/
├── bot.py                # main entry point (supports polling + webhook)
├── login.py              # one-time Telethon login (--string for Render)
├── config.py             # env loader
├── db.py                 # SQLite layer
├── user_session.py       # Telethon manager + link parser (file or StringSession)
├── topics.py             # forum topic discovery + inline keyboard
├── handlers/
│   ├── __init__.py
│   ├── admin.py          # /setgroup, /refresh, /status, etc.
│   ├── direct.py         # direct forward flow + callback dispatcher
│   └── link.py           # locked-channel URL -> fetch & forward flow
├── render.yaml           # Render Blueprint (for one-click deploy)
├── .python-version       # pins Python 3.12.0 for Render
├── requirements.txt
├── run.sh                # convenience launcher for local dev
├── .env.example
├── .gitignore
└── README.md
```

## Security checklist

- [ ] Keep `user_session.session` (local) and `SESSION_STRING` (Render) private — both grant full access to your account
- [ ] Set `ADMIN_IDS` so only you can use the bot
- [ ] Don't commit `.env` or `*.session` to git — `.gitignore` already covers them
- [ ] Use a strong, unique password on the Telegram account whose session you're using

## Roadmap (not implemented)

- Auto-mirror mode (monitor a channel and forward everything new)
- Caption editing / stripping
- Retry queue for failed forwards
- Custom keyboard column count per topic list length
- Persistent SQLite on Render (would require a paid disk)

These can be added on request.
