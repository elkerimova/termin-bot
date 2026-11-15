#!/usr/bin/env python3
"""
Checks once for an EARLIER Munich Buergerservice appointment and sends a
Telegram message if one is found. Run every few minutes by GitHub Actions
(see .github/workflows/check.yml), so no computer of yours has to be on.

Settings (GitHub repo -> Settings -> Secrets and variables -> Actions):
  TERMIN_LINK         secret: the appointment link from your confirmation e-mail
  TELEGRAM_BOT_TOKEN  secret: from @BotFather
  TELEGRAM_CHAT_ID    optional secret: your chat id (found automatically if you message the bot)
  BEFORE              optional variable: only alert for slots before this date (YYYY-MM-DD).
                      Defaults to the date of your current appointment.
  AFTER               optional variable: ignore slots before this date (YYYY-MM-DD).

It only looks for slots. It never changes or cancels your appointment.
Logs are kept free of personal details because public repos have public logs.
"""

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Berlin")
except Exception:
    TZ = None

API_CANDIDATES = [
    "https://www48.muenchen.de/buergeransicht/api/citizen",
    "https://stadt.muenchen.de/buergeransicht/api/citizen",
    "https://www.muenchen.de/buergeransicht/api/citizen",
]
BOOKING_PAGE = "https://stadt.muenchen.de/buergerservice/terminvereinbarung.html#/appointment/"
USER_AGENT = "Mozilla/5.0 (termin-bot; personal appointment checker)"
STATE_FILE = "state/state.json"


def log(msg):
    print(msg, flush=True)


def fmt(ts):
    return datetime.fromtimestamp(int(ts), TZ).strftime("%a %d.%m.%Y %H:%M")


def day_start_ts(iso):
    return int(datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=TZ).timestamp())


def parse_link(text):
    text = text.strip().strip('"').strip("'")
    if "/appointment/" in text:
        text = text.split("/appointment/", 1)[1]
    text = urllib.parse.unquote(text.split("?")[0].split("#")[0].strip("/"))
    raw = text if text.startswith("{") else base64.urlsafe_b64decode(
        text.replace("+", "-").replace("/", "_") + "=" * (-len(text) % 4)).decode("utf-8")
    data = json.loads(raw)
    return str(data["id"]), str(data["authKey"]), text


class ApiError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        codes = [e.get("errorCode", "") for e in (body.get("errors") or [])] if isinstance(body, dict) else []
        self.codes = codes
        super().__init__(f"HTTP {status}: {', '.join(codes) or str(body)[:200]}")


def http(method, url, params=None, data=None, timeout=30):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        status = e.code
    try:
        payload = json.loads(text)
    except ValueError:
        raise ApiError(status, {"errors": [{"errorCode": "notJson", "errorMessage": text[:200]}]})
    if status >= 400 or (isinstance(payload, dict) and payload.get("errors")):
        raise ApiError(status, payload)
    return payload


# ------------------------------------------------------------------ captcha (ALTCHA)
# Some offices ask the browser to do a small proof-of-work puzzle before showing
# free slots. Your browser does this silently in the background; we do the same.

def _derive(params, salt, password):
    alg = params["algorithm"].upper()
    key_len = int(params.get("keyLength") or 32)
    cost = int(params.get("cost") or 1)
    if alg in ("SHA-256", "SHA-384", "SHA-512"):
        name = alg.replace("-", "").lower()
        d = hashlib.new(name, salt + password).digest()
        for _ in range(max(1, cost) - 1):
            d = hashlib.new(name, d).digest()
        return d[:key_len]
    if alg.startswith("PBKDF2/"):
        name = alg.split("/", 1)[1].replace("-", "").lower()
        return hashlib.pbkdf2_hmac(name, password, salt, cost, key_len)
    if alg == "SCRYPT":
        r = int(params["memoryCost"])
        p = int(params.get("parallelism") or 1)
        return hashlib.scrypt(password, salt=salt, n=cost, r=r, p=p, dklen=key_len,
                              maxmem=2 * 128 * cost * r * p + 1024 * 1024)
    raise RuntimeError(f"Captcha algorithm '{alg}' is not supported by this script.")


def solve_altcha(challenge, timeout=120):
    params = challenge["parameters"]
    nonce = bytes.fromhex(params["nonce"])
    salt = bytes.fromhex(params["salt"])
    prefix = params["keyPrefix"].lower()
    prefix_bytes = bytes.fromhex(prefix) if len(prefix) % 2 == 0 else None
    start = time.time()
    counter = 0
    while True:
        key = _derive(params, salt, nonce + counter.to_bytes(4, "big"))
        if (key.startswith(prefix_bytes) if prefix_bytes is not None else key.hex().startswith(prefix)):
            took = round((time.time() - start) * 1000, 1)
            return {"challenge": challenge, "solution": {"counter": counter, "derivedKey": key.hex(), "time": took}}
        counter += 1
        if time.time() - start > timeout:
            raise RuntimeError("Captcha puzzle took too long.")


def _find_challenge(obj):
    if isinstance(obj, dict):
        if "parameters" in obj and isinstance(obj["parameters"], dict) and "nonce" in obj["parameters"]:
            return obj
        for v in obj.values():
            found = _find_challenge(v)
            if found:
                return found
    return None


def get_captcha_token(api):
    resp = http("GET", f"{api}/captcha-challenge/")
    challenge = _find_challenge(resp)
    if not challenge:
        raise RuntimeError(f"Unexpected captcha challenge format: {str(resp)[:200]}")
    solution = solve_altcha(challenge)
    payload = base64.b64encode(json.dumps(solution, separators=(",", ":")).encode()).decode()
    result = http("POST", f"{api}/captcha-verify/", data={"payload": payload})
    token = result.get("token")
    if not token:
        raise RuntimeError(f"Captcha was not accepted: {str(result)[:200]}")
    return token


# ----------------------------------------------------------------------- the API

def find_api(process_id, auth_key, preferred=None):
    candidates = ([preferred] if preferred else []) + [c for c in API_CANDIDATES if c != preferred]
    last_err = None
    for api in candidates:
        try:
            appt = http("GET", f"{api}/appointment/", {"processId": process_id, "authKey": auth_key})
            if isinstance(appt, dict) and appt.get("timestamp"):
                return api, appt
            last_err = f"{api}: unexpected answer"
        except ApiError as e:
            if e.status in (400, 404) and e.codes:
                # The API answered properly but rejected the appointment itself.
                raise SystemExit(
                    f"\nThe city's system says: {', '.join(e.codes)}.\n"
                    "Check that the link is complete and the appointment still exists.")
            last_err = f"{api}: {e}"
        except Exception as e:  # network problems, DNS, ...
            last_err = f"{api}: {e}"
    raise SystemExit(f"\nCould not reach the booking system. Last error: {last_err}\n"
                     "If this keeps happening, see 'API moved?' in the README.")


def services_of(appt):
    services = {}
    if appt.get("serviceId") and appt.get("serviceCount"):
        services[str(appt["serviceId"])] = int(appt["serviceCount"])
    for sub in appt.get("subRequestCounts") or []:
        if int(sub.get("count", 0)) > 0:
            services[str(sub["id"])] = int(sub["count"])
    return services


def earliest_slots(api, appt, end_day, token_holder):
    services = services_of(appt)
    params = {
        "startDate": date.today().isoformat(),
        "endDate": end_day.isoformat(),
        "officeIds": str(appt["officeId"]),
        "serviceIds": ",".join(services.keys()),
        "serviceCounts": ",".join(str(v) for v in services.values()),
    }
    for attempt in range(2):
        if token_holder.get("token"):
            params["captchaToken"] = token_holder["token"]
        try:
            data = http("GET", f"{api}/available-calendar/", params)
            break
        except ApiError as e:
            if attempt == 0 and any(c.startswith("captcha") for c in e.codes):
                token_holder["token"] = get_captcha_token(api)
                continue
            if any(c in ("noAppointmentForThisDay", "noAppointmentForThisScope",
                         "appointmentNotAvailable") for c in e.codes) or e.status == 404:
                return []
            raise
    slots = []
    for day in (data.get("availableDays") or []) if isinstance(data, dict) else []:
        for office in day.get("offices") or []:
            if str(office.get("officeId")) == str(appt["officeId"]) or not office.get("officeId"):
                slots.extend(int(t) for t in office.get("appointments") or [])
    return sorted(set(slots))


# ----------------------------------------------------------------------- telegram

TG_API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
_chat_id = None


def tg_call(method, params):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"{TG_API}/bot{token}/{method}", data=data, timeout=20) as r:
        return json.loads(r.read().decode())


def find_chat_id(state):
    """Use TELEGRAM_CHAT_ID if set; otherwise take it from the last message sent to the bot."""
    cid = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or state.get("chat_id")
    if cid:
        return cid
    me = tg_call("getMe", {}).get("result", {})
    hook = tg_call("getWebhookInfo", {}).get("result", {})
    updates = tg_call("getUpdates", {}).get("result", [])
    log(f"Telegram: token belongs to @{me.get('username')}; webhook set: {bool(hook.get('url'))}; "
        f"waiting messages: {len(updates)} {[k for u in updates for k in u if k != 'update_id']}")
    if hook.get("url"):
        tg_call("deleteWebhook", {})
        updates = tg_call("getUpdates", {}).get("result", [])
    for u in reversed(updates):
        msg = (u.get("message") or u.get("edited_message") or u.get("my_chat_member")
               or u.get("callback_query", {}).get("message") or {})
        if msg.get("chat", {}).get("id"):
            state["chat_id"] = str(msg["chat"]["id"])
            return state["chat_id"]
    return None


def telegram(text):
    tg_call("sendMessage", {"chat_id": _chat_id, "text": text, "disable_web_page_preview": "true"})


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


# --------------------------------------------------------------------------- main

def main():
    test_mode = "--test" in sys.argv
    process_id, auth_key, token = parse_link(os.environ["TERMIN_LINK"])
    booking_link = BOOKING_PAGE + token
    state = load_state()
    global _chat_id
    _chat_id = find_chat_id(state)
    if not _chat_id:
        log("Problem: I don't know your Telegram chat yet. Open your bot in Telegram, "
            "press Start or send it any message, then run this again.")
        return 1
    save_state(state)

    try:
        api, appt = find_api(process_id, auth_key, os.environ.get("API_URL") or state.get("api"))
    except SystemExit as e:
        msg = str(e.code).strip()
        log("Problem: " + msg)
        if "can no longer" in msg or "says:" in msg:
            if not state.get("link_dead_alerted"):
                telegram("⚠️ Termin bot: your appointment link no longer works "
                         "(rebooked or cancelled?). If you have a new link, update the TERMIN_LINK secret.\n\n" + msg)
                state["link_dead_alerted"] = True
                save_state(state)
            return 0
        if test_mode:
            telegram("❌ Termin bot test: could not reach the city's booking system from GitHub.\n" + msg)
        return 1
    state["api"] = api
    state.pop("link_dead_alerted", None)

    current = int(appt["timestamp"])
    before = os.environ.get("BEFORE", "").strip()
    after = os.environ.get("AFTER", "").strip()
    limit = min(current, day_start_ts(before)) if before else current
    floor = day_start_ts(after) if after else 0
    end_day = datetime.fromtimestamp(limit, TZ).date()
    office = appt.get("officeName") or "your office"

    if end_day < date.today():
        log("Nothing earlier left to look for.")
        save_state(state)
        return 0

    slots = [s for s in earliest_slots(api, appt, end_day, {}) if floor <= s < limit]
    log(f"Checked: {len(slots)} earlier slot(s) found.")

    if test_mode:
        telegram("✅ Termin bot is set up and working.\n"
                 f"Your appointment: {fmt(current)} ({office}).\n"
                 f"Watching for anything before {fmt(limit)}"
                 + (f", not before {after}" if after else "") + ".\n"
                 + (f"Right now there is an earlier slot: {fmt(slots[0])}" if slots else "No earlier slot right now."))

    seen = set(state.get("alerted", []))
    new = [s for s in slots if s not in seen]
    if new and not test_mode:
        lines = "\n".join("• " + fmt(s) for s in slots[:5])
        more = f"\n…and {len(slots) - 5} more" if len(slots) > 5 else ""
        telegram(f"🎉 Earlier Termin available at {office}!\n\n{lines}{more}\n\n"
                 f"Yours: {fmt(current)}\n\nRebook now (open → Termin umbuchen):\n{booking_link}")
        log("Telegram alert sent.")
    # remember what is currently on offer, so each slot is announced only once
    state["alerted"] = sorted(seen.union(slots) if not test_mode else seen)[-200:]
    save_state(state)
    return 0


def loop(minutes, every=4):
    """Check repeatedly for `minutes`, so we don't depend on GitHub's (often late) schedule."""
    end = time.time() + minutes * 60
    code = 0
    while True:
        try:
            code = main()
        except Exception as e:
            log(f"Check failed ({e}). Will retry.")
            code = 1
        if time.time() + every * 60 > end:
            return code
        time.sleep(every * 60)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        sys.exit(loop(float(sys.argv[sys.argv.index("--loop") + 1])))
    sys.exit(main())
