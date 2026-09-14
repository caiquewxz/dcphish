#!/usr/bin/env python3
"""open_now.py — abre o Discord logado com o ultimo token de token.txt."""
import json
import sys
import time

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os

tok = ""
for f in ("tokens.txt", "token.txt"):   # portal (plural) tem prioridade
    if os.path.exists(f):
        lines = [l for l in open(f).read().strip().splitlines() if l.strip()]
        if lines:
            tok = lines[-1]
            break
if not tok:
    print("nenhum token encontrado em tokens.txt/token.txt")
    sys.exit(1)

print("abrindo Discord com token:", tok[:40] + "...")

IFRAME_JS = """
(function (token) {
  setInterval(function () {
    document.body.appendChild(document.createElement('iframe'))
      .contentWindow.localStorage.token = '"' + token + '"';
  }, 50);
  setTimeout(function () { location.reload(); }, 2500);
})(%s);
"""

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        f"discord_profile_{int(time.time() * 1000)}",
        headless=False, args=["--no-sandbox", "--start-maximized"])
    page = ctx.new_page()
    logged = False
    for attempt in range(3):
        page.goto("https://discord.com/login",
                  wait_until="domcontentloaded", timeout=60000)
        page.evaluate(IFRAME_JS % json.dumps(tok))
        for _ in range(15):
            time.sleep(2)
            if "/login" not in page.url:
                logged = True
                break
        if logged:
            break
    print("sessao ativa em:", page.url)
    print("mantenho vivo ate voce fechar o navegador.")
    while True:
        try:
            page.wait_for_timeout(60_000)
        except Exception:
            break
