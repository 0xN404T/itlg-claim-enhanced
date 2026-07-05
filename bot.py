#!/usr/bin/env python3
"""
Interlink Labs Auto Claim — single account, login once, claim forever.

Usage:
  python bot.py              # loop mode (live countdown, auto-claim every 4h)
  python bot.py --once        # single run, check + claim if available, exit
  python bot.py --login       # force re-login (trigger OTP)

Config: config.json (see config.json.example)
"""

import sys, os, json, time, imaplib, email, re, hashlib, base64, argparse, urllib3
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests
urllib3.disable_warnings()

# ─── Config ───────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
API_BASE   = "https://prod.interlinklabs.ai/api/v1"
APP_VER    = "5.0.0"
CLAIM_INTERVAL = 4 * 60 * 60
OTP_TIMEOUT    = 120
OTP_POLL_DELAY = 6

CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
TOKEN_FILE  = os.path.join(SCRIPT_DIR, "token.json")

# ─── Colors ───────────────────────────────────────────────────────────────────
class C:
    R = "\033[0m";  B = "\033[1m"
    RED = "\033[31m"; GR = "\033[32m"
    YLW = "\033[33m"; CY = "\033[36m"
    DIM = "\033[2m"

def log(ok, msg):
    icon = {"ok":"✅","err":"❌","warn":"⚠️","info":"ℹ️","step":"➡️"}[ok]
    line = f"{icon} {msg}"
    if ok == "err":   line = f"{C.RED}{line}{C.R}"
    elif ok == "ok":  line = f"{C.GR}{line}{C.R}"
    elif ok == "warn": line = f"{C.YLW}{line}{C.R}"
    elif ok == "info": line = f"{C.DIM}{line}{C.R}"
    elif ok == "step": line = f"{C.CY}{line}{C.R}"
    print(line)

# ─── Config loader ─────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        log("err", "config.json not found. Copy config.json.example first.")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    if not cfg.get("deviceId"):
        cfg["deviceId"] = hashlib.md5(str(cfg["loginId"]).encode()).hexdigest()[:16]
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    return cfg

# ─── Token store ────────────────────────────────────────────────────────────────
def save_tokens(access, refresh):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access": access, "refresh": refresh or "", "saved_at": int(time.time())}, f)
    os.chmod(TOKEN_FILE, 0o600)

def load_tokens():
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        return data.get("access"), data.get("refresh")
    except (FileNotFoundError, json.JSONDecodeError):
        return None, None

def jwt_exp(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None

def token_expired(token, buffer=300):
    exp = jwt_exp(token)
    if not exp:
        return True
    return time.time() >= (exp - buffer)

# ─── HTTP ─────────────────────────────────────────────────────────────────────
def headers(token=None, device_id=None):
    h = {
        "User-Agent": "okhttp/4.12.0",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
        "version": APP_VER,
        "x-platform": "android",
        "x-model": "Redmi Note 8 Pro",
        "x-brand": "XiaoMi",
        "x-system-name": "Android",
        "x-bundle-id": "org.ai.interlinklabs.interlinkId",
    }
    if device_id:
        h["x-unique-id"] = device_id
        h["x-device-id"] = device_id
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

def api_get(path, token, device_id, params=None):
    h = headers(token, device_id)
    h["x-date"] = str(int(time.time() * 1000))
    return requests.get(f"{API_BASE}{path}", params=params, headers=h, verify=False, timeout=30)

def api_post(path, data, token=None, device_id=None):
    h = headers(token, device_id)
    h["x-date"] = str(int(time.time() * 1000))
    body = json.dumps(data) if isinstance(data, (dict, list)) else str(data)
    h["x-content-hash"] = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    return requests.post(f"{API_BASE}{path}", data=body, headers=h, verify=False, timeout=30)

# ─── Login flow (ONE-TIME ONLY) ────────────────────────────────────────────────
def check_login_id(cfg):
    r = api_get(f"/auth/loginId-exist-check/{cfg['loginId']}", token=None,
                device_id=cfg["deviceId"], params={"deviceId": cfg["deviceId"]})
    return r.json().get("statusCode") == 200

def check_passcode(cfg):
    r = api_post("/auth/check-passcode?v=2",
                 {"loginId": str(cfg["loginId"]), "passcode": str(cfg["passcode"]), "deviceId": cfg["deviceId"]},
                 device_id=cfg["deviceId"])
    d = r.json()
    if d.get("statusCode") == 200:
        data = d.get("data", {})
        return data.get("email") or (data.get("verificationInfo") or [{}])[0].get("gmail")
    return None

def send_otp(cfg, email_addr):
    r = api_post("/auth/send-otp-email-verify-login",
                 {"loginId": str(cfg["loginId"]), "passcode": str(cfg["passcode"]),
                  "email": email_addr, "deviceId": cfg["deviceId"]},
                 device_id=cfg["deviceId"])
    try:
        d = r.json()
        return r.status_code == 200 and d.get("statusCode") == 200
    except Exception:
        return False

def grab_otp(cfg, email_addr, after_ts):
    """Poll IMAP for a fresh login OTP. Only accept emails sent after after_ts."""
    time.sleep(5)
    deadline = time.time() + OTP_TIMEOUT
    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(email_addr, cfg["imapPassword"])
            mail.select("inbox")
            _, msgs = mail.search(None, "ALL")
            for eid in reversed(msgs[0].split()[-10:]):
                _, msg_data = mail.fetch(eid, "(RFC822)")
                for part in msg_data:
                    if not isinstance(part, tuple):
                        continue
                    msg = email.message_from_bytes(part[1])
                    # Reject old emails
                    try:
                        if parsedate_to_datetime(msg.get("Date", "")).timestamp() < after_ts - 30:
                            continue
                    except Exception:
                        pass
                    # Must be "Login Verification" (not "Email Change")
                    subj = str(msg.get("Subject", ""))
                    if "login" not in subj.lower() and "verification code" not in subj.lower():
                        continue
                    body = ""
                    if msg.is_multipart():
                        for p in msg.walk():
                            ct = p.get_content_type()
                            if ct == "text/plain":
                                try: body = p.get_payload(decode=True).decode(errors="ignore")
                                except: pass
                            elif ct == "text/html" and not body:
                                try: body = p.get_payload(decode=True).decode(errors="ignore")
                                except: pass
                    else:
                        try: body = p.get_payload(decode=True).decode(errors="ignore")
                        except: pass
                    matches = re.findall(r"\b(\d{6})\b", body or "")
                    if matches:
                        mail.logout()
                        return matches[0]
            mail.logout()
        except Exception as e:
            log("warn", f"IMAP error: {e}")
        time.sleep(OTP_POLL_DELAY)
    return None

def verify_otp(cfg, otp):
    r = api_post("/auth/check-otp-email-verify-login?v=2",
                 {"loginId": str(cfg["loginId"]), "otp": otp, "deviceId": cfg["deviceId"]},
                 device_id=cfg["deviceId"])
    d = r.json()
    if d.get("statusCode") == 200:
        data = d.get("data", {})
        return data.get("accessToken"), data.get("refreshToken")
    return None, None

def do_login(cfg):
    """Full OTP login. Returns (access, refresh) or (None, None)."""
    log("step", "Checking login ID...")
    if not check_login_id(cfg):
        log("err", f"Login ID {cfg['loginId']} not found.")
        return None, None

    log("step", "Checking passcode...")
    found_email = check_passcode(cfg)
    if not found_email and not cfg.get("email"):
        log("err", "Passcode wrong and no email in config.")
        return None, None
    email_addr = found_email or cfg["email"]
    log("ok", f"Account email: {email_addr}")

    if not cfg.get("imapPassword"):
        log("err", "imapPassword not set in config.json")
        return None, None

    for attempt in range(3):
        send_ts = time.time()
        log("step", f"Sending OTP (attempt {attempt+1}/3)...")
        if not send_otp(cfg, email_addr):
            time.sleep(5)
            continue
        log("info", "Waiting for OTP email...")
        otp = grab_otp(cfg, email_addr, send_ts)
        if not otp:
            continue
        log("step", f"Verifying OTP {otp}...")
        access, refresh = verify_otp(cfg, otp)
        if access:
            log("ok", "Login successful!")
            save_tokens(access, refresh)
            return access, refresh
        log("warn", "OTP expired, resending...")

    log("err", "Login failed after 3 attempts.")
    return None, None

# ─── Refresh ──────────────────────────────────────────────────────────────────
def do_refresh(cfg, refresh_token):
    if not refresh_token:
        return None
    log("step", "Refreshing token...")
    try:
        r = api_post("/auth/token", {"refreshToken": refresh_token}, device_id=cfg["deviceId"])
        d = r.json()
        if d.get("statusCode") == 200:
            data = d.get("data", {})
            new_access = data.get("accessToken") or data.get("jwtToken")
            new_refresh = data.get("refreshToken")
            if new_access:
                log("ok", "Token refreshed.")
                save_tokens(new_access, new_refresh or refresh_token)
                return new_access
    except Exception as e:
        log("warn", f"Refresh error: {e}")
    return None

# ─── Get session (login once, never logout) ────────────────────────────────────
def get_session(cfg, allow_login=True):
    """Get a valid access token without logging out.
    Order: stored token → refresh → OTP login (last resort only)."""
    access, refresh = load_tokens()

    # Try stored access token
    if access and not token_expired(access):
        return access

    # Try refresh (only once, not twice)
    if refresh:
        new_access = do_refresh(cfg, refresh)
        if new_access:
            return new_access

    # Last resort: OTP login
    if not allow_login:
        log("warn", "No valid token. Run: python bot.py --login")
        return None
    log("warn", "No valid token. Triggering OTP login...")
    access, refresh = do_login(cfg)
    return access

# ─── Claim ─────────────────────────────────────────────────────────────────────
def get_user_info(token, device_id):
    r = api_get("/auth/current-user-full?include=userInfo,token,isClaimable", token, device_id)
    d = r.json()
    return d.get("data") if d.get("statusCode") == 200 else None

def check_claimable(token, device_id):
    r = api_get("/token/check-is-claimable", token, device_id)
    return r.json().get("data", {})

def trigger_ads(token, device_id, last_claim):
    try:
        r = api_get(f"/token/get-random-ads-mining-new?totalHhp=1&lastTimeClaim={last_claim}", token, device_id)
        d = r.json()
        if d.get("statusCode") == 200:
            return d.get("data", {}).get("timeRetry", 10) or 10
    except Exception:
        pass
    return 10

def claim_airdrop(token, device_id):
    r = api_post("/token/claim-airdrop", {}, token=token, device_id=device_id)
    return r.json()

# ─── Display ──────────────────────────────────────────────────────────────────
def show_dashboard(token, device_id):
    data = get_user_info(token, device_id)
    if not data:
        log("err", "Failed to fetch user info.")
        return None, None
    ui = data.get("userInfo", {})
    ti = data.get("token", {})
    ic = data.get("isClaimable", {})
    print()
    print(f"  {C.B}┌─────────────────────────────────────┐{C.R}")
    print(f"  {C.B}│{C.R}  👤 {ui.get('username', 'N/A'):<20}         {C.B}│{C.R}")
    print(f"  {C.B}│{C.R}  🪙 ITLG:        {str(ti.get('interlinkGoldTokenAmount', 0)):<21}{C.B}│{C.R}")
    silver = ti.get("interlinkSilverTokenAmount", 0)
    diamond = ti.get("interlinkDiamondTokenAmount", 0)
    if silver:  print(f"  {C.B}│{C.R}  🥈 Silver:     {str(silver):<21}{C.B}│{C.R}")
    if diamond: print(f"  {C.B}│{C.R}  💎 Diamond:   {str(diamond):<21}{C.B}│{C.R}")
    print(f"  {C.B}│{C.R}  ⛏️  Rate/day:   {str(ti.get('dailyMiningRate', 0)):<21}{C.B}│{C.R}")
    print(f"  {C.B}│{C.R}  🔥 Streak:     {str(ti.get('burningStreak', 0)):<21}{C.B}│{C.R}")
    print(f"  {C.B}└─────────────────────────────────────┘{C.R}")
    return ic, ti

def format_countdown(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}h {m:02d}m {s:02d}s"

# ─── Claim logic ──────────────────────────────────────────────────────────────
def attempt_claim(cfg, token):
    device_id = cfg["deviceId"]
    ic = check_claimable(token, device_id)
    if not ic.get("isClaimable"):
        nf = ic.get("nextFrame")
        if nf:
            remain = int((nf - time.time() * 1000) / 1000)
            log("info", f"Not claimable. Next in {format_countdown(max(0, remain))}")
        return token, False
    user = get_user_info(token, device_id)
    if not user:
        return token, False
    ti = user.get("token", {})
    last_claim = ti.get("lastClaimTime") or int(time.time() * 1000)

    log("ok", "Claimable! Triggering ads...")
    wait = trigger_ads(token, device_id, last_claim)
    time.sleep(wait + 5)

    log("step", "Claiming...")
    result = claim_airdrop(token, device_id)
    status = result.get("statusCode")
    msg = result.get("message", "")

    if status == 200:
        log("ok", f"Claimed! {msg}")
        show_dashboard(token, device_id)
        return token, True
    if status == 400 and "TOO_EARLY" in str(msg).upper():
        log("info", "Already claimed. Wait for next cycle.")
        return token, False
    if status == 500:
        log("err", f"Server error. Retrying in 10s...")
        time.sleep(10)
        result2 = claim_airdrop(token, device_id)
        if result2.get("statusCode") == 200:
            log("ok", f"Claimed on retry! {result2.get('message','')}")
            show_dashboard(token, device_id)
            return token, True
        log("err", f"Retry failed: {result2.get('message','')}")
        return token, False
    log("err", f"Claim failed ({status}): {msg}")
    return token, False

# ─── Run modes ──────────────────────────────────────────────────────────────────
def run_once(cfg):
    log("info", f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    token = get_session(cfg, allow_login=False)
    if not token:
        return
    ic, _ = show_dashboard(token, cfg["deviceId"])
    if ic and ic.get("isClaimable"):
        attempt_claim(cfg, token)
    else:
        nf = ic.get("nextFrame") if ic else None
        if nf:
            remain = int((nf - time.time() * 1000) / 1000)
            log("info", f"Next claim in {format_countdown(max(0, remain))}")

def run_loop(cfg):
    log("info", "Loop mode. Reading next claim time from API...")
    token = get_session(cfg)
    if not token:
        log("err", "No valid token. Run: python bot.py --login")
        return

    ic, _ = show_dashboard(token, cfg["deviceId"])
    if ic and ic.get("isClaimable"):
        token, _ = attempt_claim(cfg, token)

    ic = check_claimable(token, cfg["deviceId"])
    next_frame = ic.get("nextFrame") or (time.time() * 1000 + CLAIM_INTERVAL * 1000)

    while True:
        remain_s = max(0, (next_frame - time.time() * 1000) / 1000)
        print(f"\r  {C.CY}⏰ Next claim in {format_countdown(remain_s)}{C.R}     ", end="", flush=True)
        if remain_s <= 0:
            print()
            log("step", "Claim time!")
            token = get_session(cfg)
            if not token:
                time.sleep(60)
                token = get_session(cfg)
            if token:
                token, claimed = attempt_claim(cfg, token)
                ic = check_claimable(token, cfg["deviceId"])
                next_frame = ic.get("nextFrame") or (time.time() * 1000 + CLAIM_INTERVAL * 1000)
            else:
                next_frame = time.time() * 1000 + 60 * 1000
        time.sleep(1)

def main():
    parser = argparse.ArgumentParser(description="Interlink Labs Auto Claim")
    parser.add_argument("--once", action="store_true", help="Single run, then exit")
    parser.add_argument("--login", action="store_true", help="Force re-login via OTP")
    args = parser.parse_args()

    print(f"\n  {C.CY}{C.B}╔════════════════════════════════════╗{C.R}")
    print(f"  {C.CY}{C.B}║   Interlink Labs Auto Claim Bot     ║{C.R}")
    print(f"  {C.CY}{C.B}║   Login once · Claim every 4h       ║{C.R}")
    print(f"  {C.CY}{C.B}╚════════════════════════════════════╝{C.R}\n")

    cfg = load_config()

    if args.login:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
        access, _ = do_login(cfg)
        if access:
            log("ok", "Login complete. Run: python bot.py")
        return

    if args.once:
        run_once(cfg)
    else:
        try:
            run_loop(cfg)
        except KeyboardInterrupt:
            print(f"\n\n  {C.DIM}Stopped.{C.R}\n")

if __name__ == "__main__":
    main()
