#!/usr/bin/env python3
"""
listener.py — assina o topico ntfy.sh e abre o Discord logado no navegador
quando o portal publica o token capturado (canal pela internet).

Uso:  python listener.py
"""
import json
import sys
import threading
import time

import requests
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import TOPIC

URL = f"https://ntfy.sh/{TOPIC}/sse"

contexts = {}   # mantem os contextos vivos (navegador aberto)

seen = set()            # ids ja processados
last_id = ["all"]       # ultimo id visto (catch-up apos queda)


def open_discord(token):
    # perfil unico por sessao: evita o lock do Chrome ("abrindo em uma
    # sessao de navegador existente") quando uma janela anterior esta viva
    profile = f"discord_profile_{int(time.time() * 1000)}"

    def run():
        IFRAME_JS = """
        (function (token) {
          setInterval(function () {
            document.body.appendChild(document.createElement('iframe'))
              .contentWindow.localStorage.token = '"' + token + '"';
          }, 50);
          setTimeout(function () { location.reload(); }, 2500);
        })(%s);
        """
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    profile, headless=False,
                    args=["--no-sandbox", "--start-maximized"])
                contexts[id(ctx)] = ctx
                page = ctx.new_page()
                logged = False
                for attempt in range(3):
                    page.goto("https://discord.com/login",
                              wait_until="domcontentloaded", timeout=60000)
                    page.evaluate(IFRAME_JS % json.dumps(token))
                    for _ in range(15):
                        time.sleep(2)
                        if "/login" not in page.url:
                            logged = True
                            break
                    if logged:
                        break
                print("sessao ativa em:", page.url)
                while True:
                    try:
                        page.wait_for_timeout(60_000)
                    except Exception:
                        break
        except Exception as e:
            print("erro no navegador:", repr(e)[:300])

    threading.Thread(target=run, daemon=True).start()


def process_msg(m):
    mid = m.get("id")
    if not mid or mid in seen:
        return
    seen.add(mid)
    last_id[0] = mid
    token = (m.get("message") or "").strip()
    if token.startswith("ERR:"):
        print("\n[!] ERRO do M5:", token[:300])
        return
    if token.startswith("MTA"):
        print("\n[+] TOKEN recebido:", token)
        with open("token.txt", "a") as f:
            f.write(token + "\n")
        open_discord(token)


def main():
    print(f"[*] assinando ntfy.sh/{TOPIC} — aguardando token...")
    while True:
        try:
            # fase SSE: mensagens em tempo real
            with requests.get(URL, stream=True, timeout=3600) as r:
                for line in r.iter_lines():
                    if not line or not line.startswith(b"data:"):
                        continue
                    try:
                        m = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    if m.get("event") == "message":
                        process_msg(m)
        except Exception as e:
            print("sse erro:", repr(e)[:120])
        # catch-up: pega mensagens publicadas durante a queda
        try:
            r = requests.get(
                f"https://ntfy.sh/{TOPIC}/json?since={last_id[0]}",
                timeout=20)
            if r.status_code == 200:
                msgs = r.json()
                if isinstance(msgs, dict):
                    process_msg(msgs)
                elif isinstance(msgs, list):
                    for m in msgs:
                        process_msg(m)
        except Exception as e:
            print("catch-up erro:", repr(e)[:120])
        threading.Event().wait(2)


if __name__ == "__main__":
    main()
