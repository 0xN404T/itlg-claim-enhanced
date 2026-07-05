# Interlink Auto Claim

Auto-claim $ITLG from Interlink Labs. Every 4 hours, no manual clicking.

Single Python script. Login once with IMAP OTP, then it claims forever. The bot knows when your next claim is available and counts down automatically — just leave it running.

## Setup

```bash
pip install requests
cp config.json.example config.json
```

Edit `config.json`:

```json
{
  "loginId": "123456",
  "passcode": "000000",
  "email": "your-email@gmail.com",
  "imapPassword": "your gmail app password",
  "deviceId": ""
}
```

- `loginId` — your Interlink ID (number, not email)
- `passcode` — 6-digit passcode you set during registration
- `email` — the email registered to your Interlink account
- `imapPassword` — Gmail App Password ([get one here](https://myaccount.google.com/apppasswords), not your Gmail password)
- `deviceId` — leave empty, it auto-generates

## Run

```bash
python bot.py
```

That's it. The bot logs in via OTP once, saves the token, and stays running. It reads the next claim time from the API and shows a live countdown:

```
  ⏰ Next claim in 03h 52m 10s
```

When the timer hits zero, it claims automatically and resets the countdown.

## Options

```
python bot.py          # run with live countdown timer
python bot.py --once   # single run, check + claim if available, then exit
python bot.py --login  # force re-login (get new OTP)
```

## Saving Your Token

After your first login, the bot saves your token to `token.json`. This file lets you claim without logging in again. Keep a backup somewhere safe — if you lose it, you'll need to re-login via OTP.

**Backup:**
```bash
cp token.json ~/token-backup.json
```

**Restore:**
```bash
cp ~/token-backup.json token.json
chmod 600 token.json
```

You can also manually paste a token from packet capture (e.g. HTTP Catcher on your phone):
```bash
echo '{"access":"eyJ...","refresh":"eyJ...","saved_at":0}' > token.json
chmod 600 token.json
```

Only `access` is required. `refresh` is optional but helps auto-renew.

## How It Works

1. First run: sends OTP to your Gmail, IMAP grabs it, verifies, saves token
2. Bot reads `nextFrame` from the API — knows exactly when you can claim next
3. Counts down in real-time. When timer hits zero: checks claimable, triggers ads session, claims
4. Token never logs out. If expired, refreshes automatically. Only re-logins if refresh token also dies.

## Token Types

| App Display | Token | What it is |
|---|---|---|
| Gold | **ITLG** | What you mine. This is the airdrop token. |
| Interlink | ITL | Utility token, separate supply |
| Silver / Diamond | — | Boosters, not separate tokens |

You're mining **ITLG**.

## Files

```
bot.py              # the bot
config.json         # your config (gitignored)
config.json.example # template
token.json          # saved token (gitignored, auto-created)
```

## Notes

- Your token is saved locally with `chmod 600`. Don't share `config.json` or `token.json`.
- If OTP doesn't arrive, the bot resends automatically (up to 3 times).
- No multi-account, no proxy rotation, no Node.js. One script, one account, one dependency.

## License

MIT
