# Termin bot: a Telegram alert when an earlier Munich appointment opens up

This bot checks every ~5 minutes whether a slot has opened up **before your current appointment**. If it finds one, it sends you a Telegram message with the new times and your rebooking link.
It runs for free on GitHub's servers, so your laptop can stay off.
It only looks for slots. It never changes or cancels your appointment.

It reads your appointment date from your link, so it automatically looks for anything before **07.01**.

Setup takes about 10 minutes, and you only do it once.

---

## Step 1: Create your Telegram bot (2 min)

1. In Telegram, open **@BotFather**, send `/newbot`, and pick a name and a username. Copy the **token** it gives you.
2. Open your new bot (search for its username) and press **Start**, or send it any message.
   The bot uses that message to find out where to send your alerts.

## Step 2: Put the bot on GitHub (5 min)

1. Sign up at https://github.com (free) if you don't have an account.
2. Click **+** (top right) → **New repository**. Name it e.g. `termin-bot` and choose **Public**.
   Public repos get unlimited free run time. Your link, token and chat ID go into
   encrypted *secrets* (Step 3), and nobody else can see those.
3. On the new repo page, click **uploading an existing file** and drag in `check.py` and `README.md`. Click **Commit changes**.
4. Click **Add file → Create new file**. In the name box, type exactly
   `.github/workflows/check.yml`
   (the slashes create the folders). Paste the full content of `check.yml` from this zip and click **Commit changes**.

## Step 3: Add your secrets (2 min)

In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add these:

| Name | Value |
|---|---|
| `TERMIN_LINK` | the full appointment link from your confirmation e-mail |
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |

## Step 4: Test it

Open the **Actions** tab (click the green button to enable workflows if GitHub asks) → **Check for earlier Termin** → **Run workflow** → **Run workflow**.

Within a minute you should get a Telegram message: **"✅ Termin bot is set up and working"**, with your appointment date.
From then on it checks every 5 minutes on its own and only messages you when there's an earlier slot.

When you get an alert, open the link, choose **Termin umbuchen** and pick the new time. Be quick, because slots that free up go fast.

---

## Good to know

- **Optional filters:** under **Settings → Secrets and variables → Actions → Variables** you can add
  `AFTER` = `2026-11-20` to ignore slots that are too soon for you, or `BEFORE` = `2026-12-20` to only get alerts before that date.
- **Timing:** GitHub sometimes runs scheduled checks a few minutes late, especially at busy times.
- **After you rebook:** the old link may stop working. The bot will tell you once in Telegram. Put the new link into the `TERMIN_LINK` secret if you want to keep looking for something even earlier.
- **To stop it:** Actions → Check for earlier Termin → **⋯** → **Disable workflow**. Or delete the repo.
- **If the test run fails:** open the failed run in the Actions tab and read the red step. If it says it *could not reach the booking system*, the city may be blocking GitHub's servers. Send me the message and we'll find another place to run it.
- GitHub pauses scheduled jobs in repos that have had no activity for 60 days. That won't matter before 07.01.
