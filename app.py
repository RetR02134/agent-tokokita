"""TokoKita web chat.

Alur: browser -> /api/chat -> model (Azure OpenAI Responses API, API key)
      -> bila model minta data toko, aplikasi ini memanggil APIM (/POS/mcp) dengan
      Ocp-Apim-Subscription-Key -> hasilnya dikirim balik ke model -> jawaban dialirkan (SSE).

Keamanan (ringkas):
- Konfigurasi dari environment / file .env (lihat .env.example). Tidak ada rahasia di kode.
- ID percakapan model disimpan di SERVER, terikat ke cookie sesi bertanda tangan. Klien tidak bisa
  memasukkan ID percakapan sendiri.
- Argumen tool divalidasi di server (format, rentang, dan nomor HP / 4 digit terakhir harus pernah
  diketik pelanggan sendiri di percakapan ini). Tidak hanya mengandalkan prompt.
- Rate limit per IP (dengan ProxyFix), batas percobaan verifikasi, dan batas harian.
- State sesi disimpan di memori proses: jalankan SATU worker gunicorn (skala lewat thread).
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory, session, stream_with_context
from openai import OpenAI
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("tokokita-web")

BUILD = "2026-11-10-hardened"


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _env_int(name, default):
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


# --- Konfigurasi -----------------------------------------------------------------
APP_ENV = _env("APP_ENV", "production").lower()
AZURE_OPENAI_ENDPOINT = _env("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = _env("AZURE_OPENAI_API_KEY")
MODEL_DEPLOYMENT = _env("MODEL_DEPLOYMENT", "gpt-5-mini")
MCP_SERVER_URL = _env("MCP_SERVER_URL")
MCP_SUBSCRIPTION_KEY = _env("MCP_SUBSCRIPTION_KEY")
MCP_TOOL_NAME = _env("MCP_TOOL_NAME", "whenAnHttpRequestIsReceivedInvoke")
REASONING_EFFORT = _env("REASONING_EFFORT", "low")  # kosongkan untuk mematikan

MAX_MESSAGE_CHARS = _env_int("MAX_MESSAGE_CHARS", 1500)
RATE_LIMIT = _env_int("RATE_LIMIT_PER_MIN", 15)                 # pesan / menit / IP
DAILY_MESSAGE_LIMIT = _env_int("DAILY_MESSAGE_LIMIT", 2000)     # semua pengguna, per hari
MAX_TURNS_PER_CONVERSATION = _env_int("MAX_TURNS_PER_CONVERSATION", 40)
VERIFY_PER_SESSION = _env_int("VERIFY_ATTEMPTS_PER_SESSION", 10)  # per 15 menit
VERIFY_PER_IP = _env_int("VERIFY_ATTEMPTS_PER_IP", 30)            # per 15 menit
MAX_QTY = _env_int("MAX_QTY", 50)
TOOL_OUTPUT_MAX = _env_int("TOOL_OUTPUT_MAX", 12000)
MAX_TOOL_ROUNDS = 6
SESSION_TTL = _env_int("SESSION_TTL_SECONDS", 8 * 3600)
MAX_SESSIONS = _env_int("MAX_SESSIONS", 5000)
TRUSTED_PROXIES = _env_int("TRUSTED_PROXIES", 1)  # jumlah proxy tepercaya di depan app (0 = langsung terbuka)
HEALTH_TOKEN = _env("HEALTH_TOKEN")
ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in _env("ALLOWED_ORIGINS").split(",") if o.strip()}

SECRET_KEY = _env("SECRET_KEY")
if not SECRET_KEY:
    if APP_ENV == "dev":
        SECRET_KEY = secrets.token_hex(32)
        log.warning("SECRET_KEY kosong; memakai kunci acak (sesi hilang saat restart). Hanya untuk APP_ENV=dev.")
    else:
        raise RuntimeError("SECRET_KEY wajib diisi (lihat .env.example). Buat dengan: python -c \"import secrets; print(secrets.token_hex(32))\"")
if not MCP_SERVER_URL:
    log.warning("MCP_SERVER_URL kosong; panggilan ke POS akan gagal.")

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "instructions.md"), encoding="utf-8") as f:
    INSTRUCTIONS = f.read()

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config.update(
    SECRET_KEY=SECRET_KEY,
    MAX_CONTENT_LENGTH=16 * 1024,
    SESSION_COOKIE_NAME="tk_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_env("COOKIE_SECURE", "1") != "0",
    PERMANENT_SESSION_LIFETIME=SESSION_TTL,
    JSON_AS_ASCII=False,
)
if TRUSTED_PROXIES > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXIES, x_proto=TRUSTED_PROXIES,
                            x_host=TRUSTED_PROXIES)

ACTIONS = ["search_product", "get_product", "compare_products", "create_order",
           "mark_paid", "get_order", "cancel_order", "list_orders"]
FIELDS = ["query", "sku", "skus", "qty", "customer_name", "phone", "address", "email",
          "order_id", "phone_last4"]
REQUIRED = {
    "search_product": ["query"],
    "get_product": ["sku"],
    "compare_products": ["skus"],
    "create_order": ["sku", "qty", "customer_name", "phone", "address", "email"],
    "mark_paid": ["order_id", "phone_last4"],
    "get_order": ["order_id", "phone_last4"],
    "cancel_order": ["order_id", "phone_last4"],
    "list_orders": ["phone", "customer_name"],
}
READ_ONLY = {"search_product", "get_product", "compare_products", "get_order", "list_orders"}
VERIFY_ACTIONS = {"mark_paid", "get_order", "cancel_order", "list_orders"}

POS_TOOL = {
    "type": "function",
    "name": "pos_api",
    "description": "API POS TokoKita untuk produk, pesanan, dan pembayaran. Isi 'action' dan hanya field yang diperlukan action itu.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ACTIONS},
            "query": {"type": "string", "description": "Kata kunci untuk search_product"},
            "sku": {"type": "string", "description": "Kode SKU, contoh KITAMIX-600"},
            "skus": {"type": "string", "description": "2-3 SKU dipisah koma untuk compare_products"},
            "qty": {"type": "integer", "description": "Jumlah barang"},
            "customer_name": {"type": "string"},
            "phone": {"type": "string", "description": "Nomor HP lengkap"},
            "address": {"type": "string"},
            "email": {"type": "string"},
            "order_id": {"type": "string", "description": "Contoh TK-1042"},
            "phone_last4": {"type": "string", "description": "4 digit terakhir HP"},
        },
        "required": ["action"],
    },
}


def err_json(code, detail):
    return json.dumps({"error": code, "detail": detail}, ensure_ascii=False)


# --- Rate limiter, batas harian, dan state sesi (memori proses) ---------------------
class SlidingLimiter:
    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_gc = time.time()

    def hit(self, key):
        """True bila masih diizinkan (dan mencatat satu hit)."""
        now = time.time()
        with self._lock:
            if now - self._last_gc > 60:
                self._last_gc = now
                for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]:
                    del self._hits[k]
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


chat_limiter = SlidingLimiter(RATE_LIMIT, 60)
deep_health_limiter = SlidingLimiter(3, 60)
verify_ip_limiter = SlidingLimiter(VERIFY_PER_IP, 900)

_daily = {"day": None, "n": 0}
_daily_lock = threading.Lock()


def daily_budget_ok():
    today = date.today().isoformat()
    with _daily_lock:
        if _daily["day"] != today:
            _daily["day"], _daily["n"] = today, 0
        if _daily["n"] >= DAILY_MESSAGE_LIMIT:
            return False
        _daily["n"] += 1
        return True


_sessions = {}
_sessions_lock = threading.Lock()


def get_state(sid):
    now = time.time()
    with _sessions_lock:
        if len(_sessions) > MAX_SESSIONS:
            for k in [k for k, v in _sessions.items() if now - v["seen"] > SESSION_TTL]:
                del _sessions[k]
            while len(_sessions) > MAX_SESSIONS:  # buang yang paling lama tidak aktif
                del _sessions[min(_sessions, key=lambda k: _sessions[k]["seen"])]
        st = _sessions.get(sid)
        if st is None or now - st["seen"] > SESSION_TTL:
            st = {"conv": None, "turns": 0, "ids": deque(maxlen=24), "verify": deque(),
                  "orders": {}, "busy": False, "seen": now}
            _sessions[sid] = st
        st["seen"] = now
        return st


def drop_state(sid):
    with _sessions_lock:
        _sessions.pop(sid, None)


def client_ip():
    ip = request.remote_addr or "-"
    if ip.count(":") == 1:  # "1.2.3.4:5678" (sebagian proxy menyertakan port)
        ip = ip.split(":")[0]
    return ip


# --- Identitas: nomor HP / 4 digit harus berasal dari pelanggan --------------------------
def _hash(s):
    return hmac.new(SECRET_KEY.encode(), s.encode(), hashlib.sha256).hexdigest()[:16]


def _norm_digits(s):
    d = re.sub(r"\D", "", str(s))
    if d.startswith("62"):
        d = "0" + d[2:]
    return d


def remember_identity(st, message):
    """Catat (sebagai hash) nomor HP dan angka 4 digit yang diketik pelanggan di pesan ini."""
    text = re.sub(r"(?i)\bTK-\d+\b", " ", message)  # nomor pesanan bukan identitas
    for m in re.findall(r"\+?\d[\d\s().-]{2,}\d", text):
        d = _norm_digits(m)
        if len(d) == 4:
            st["ids"].append("l4:" + _hash(d))
        elif 8 <= len(d) <= 15:
            st["ids"].append("ph:" + _hash(d))
            st["ids"].append("l4:" + _hash(d[-4:]))


def identity_ok(st, clean):
    ids = set(st["ids"])
    if "phone" in clean and "ph:" + _hash(_norm_digits(clean["phone"])) not in ids:
        return False
    if "phone_last4" in clean and "l4:" + _hash(_norm_digits(clean["phone_last4"])) not in ids:
        return False
    return True


# --- Validasi argumen tool (di server, bukan hanya di prompt) ---------------------------------
RE_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
RE_ORDER = re.compile(r"^TK-\d{1,10}$", re.I)
RE_SKU = re.compile(r"^[A-Za-z0-9._-]{2,40}$")
RE_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+$")


def _s(v, lo, hi):
    if not isinstance(v, str):
        v = str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else ""
    v = RE_CTRL.sub("", v).strip()
    return v if lo <= len(v) <= hi else None


def validate_call(args):
    """Mengembalikan (argumen_bersih, None) atau (None, pesan_error_untuk_model)."""
    if not isinstance(args, dict):
        return None, err_json("INVALID_ARGUMENT", "argumen tidak valid")
    action = args.get("action")
    if action not in ACTIONS:
        return None, err_json("INVALID_ARGUMENT", "action tidak dikenal")
    clean = {"action": action}
    for f in REQUIRED[action]:
        v = args.get(f)
        if v in (None, ""):
            return None, err_json("INVALID_ARGUMENT", f"field '{f}' wajib diisi")
        if f == "qty":
            try:
                if isinstance(v, bool):
                    raise ValueError
                q = int(v)
            except (TypeError, ValueError):
                return None, err_json("INVALID_ARGUMENT", "qty harus bilangan bulat")
            if not 1 <= q <= MAX_QTY:
                return None, err_json("INVALID_ARGUMENT", f"qty harus 1-{MAX_QTY}; untuk jumlah lebih besar arahkan ke tim CS")
            clean[f] = q
        elif f == "sku":
            v = _s(v, 2, 40)
            if not v or not RE_SKU.match(v):
                return None, err_json("INVALID_ARGUMENT", "format SKU tidak valid")
            clean[f] = v
        elif f == "skus":
            parts = [p.strip() for p in str(v).split(",") if p.strip()]
            if not 2 <= len(parts) <= 3 or not all(RE_SKU.match(p) for p in parts):
                return None, err_json("INVALID_ARGUMENT", "skus harus 2-3 SKU valid dipisah koma")
            clean[f] = ",".join(parts)
        elif f == "query":
            v = _s(v, 1, 100)
            if not v:
                return None, err_json("INVALID_ARGUMENT", "kata kunci tidak valid")
            clean[f] = v
        elif f == "customer_name":
            v = _s(v, 2, 100)
            if not v:
                return None, err_json("INVALID_ARGUMENT", "nama harus 2-100 karakter")
            clean[f] = v
        elif f == "phone":
            d = _norm_digits(v)
            if not 8 <= len(d) <= 15:
                return None, err_json("INVALID_ARGUMENT", "nomor HP harus 8-15 digit")
            clean[f] = _s(v, 8, 25) or d
        elif f == "address":
            v = _s(v, 15, 300)
            if not v:
                return None, err_json("INVALID_ARGUMENT", "alamat harus 15-300 karakter")
            clean[f] = v
        elif f == "email":
            v = _s(v, 5, 120)
            if not v or not RE_EMAIL.match(v):
                return None, err_json("INVALID_EMAIL", "alamat email tidak valid; minta pelanggan memperbaikinya")
            clean[f] = v
        elif f == "order_id":
            v = _s(v, 4, 14)
            if not v or not RE_ORDER.match(v):
                return None, err_json("INVALID_ARGUMENT", "nomor pesanan harus berformat TK-angka")
            clean[f] = v
        elif f == "phone_last4":
            v = _s(v, 4, 4)
            if not v or not v.isdigit():
                return None, err_json("INVALID_ARGUMENT", "phone_last4 harus 4 digit")
            clean[f] = v
    return clean, None


# --- Panggilan ke POS lewat APIM ----------------------------------------------------------
def _mcp_post(payload, timeout=30):
    return requests.post(
        MCP_SERVER_URL,
        json=payload,
        timeout=timeout,
        headers={
            "Ocp-Apim-Subscription-Key": MCP_SUBSCRIPTION_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )


def _parse_mcp_body(resp):
    """Balasan MCP bisa JSON biasa atau SSE ('data: {...}')."""
    text = resp.text or ""
    ctype = resp.headers.get("Content-Type", "")
    if "text/event-stream" in ctype or text.lstrip().startswith(("event:", "data:")):
        last = None
        for line in text.splitlines():
            if line.startswith("data:"):
                last = line[5:].strip()
        text = last or ""
    return json.loads(text)


def call_pos(clean):
    """Satu panggilan POS (argumen sudah divalidasi). Selalu mengembalikan string untuk model."""
    action = clean["action"]
    attempts = 2 if action in READ_ONLY else 1  # jangan ulangi aksi yang mengubah data
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": MCP_TOOL_NAME, "arguments": clean}}
    for n in range(attempts):
        try:
            resp = _mcp_post(payload)
            if resp.status_code >= 500 and n + 1 < attempts:
                time.sleep(0.5)
                continue
            if resp.status_code != 200:
                log.error("POS action=%s HTTP %s", action, resp.status_code)  # isi balasan tidak dicatat (PII)
                return err_json("POS_UNAVAILABLE", f"POS tidak dapat dihubungi (HTTP {resp.status_code})")
            body = _parse_mcp_body(resp)
            if "error" in body:
                log.error("POS action=%s mengembalikan error JSON-RPC", action)
                return err_json("POS_ERROR", "POS mengembalikan kesalahan")
            result = body.get("result", {})
            parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
            out = "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)
            if result.get("isError"):
                log.warning("POS action=%s isError=true", action)
            return out[:TOOL_OUTPUT_MAX]
        except requests.RequestException as e:
            log.error("POS action=%s gagal: %s", action, type(e).__name__)
            if n + 1 >= attempts:
                return err_json("POS_UNAVAILABLE", "POS tidak dapat dihubungi")
            time.sleep(0.5)
        except Exception:
            log.exception("Gagal memproses balasan POS action=%s", action)
            return err_json("POS_UNAVAILABLE", "POS tidak dapat dihubungi")
    return err_json("POS_UNAVAILABLE", "POS tidak dapat dihubungi")


def run_tool(args, st, ip):
    """Validasi + pagar keamanan + panggil POS. Dijalankan di thread pool."""
    clean, err = validate_call(args)
    if err:
        return err
    action = clean["action"]
    if action in VERIFY_ACTIONS or action == "create_order":
        if not identity_ok(st, clean):
            return err_json("VERIFIKASI_DITOLAK",
                            "Nomor HP / 4 digit terakhir harus diketik sendiri oleh pelanggan di percakapan ini. "
                            "Minta pelanggan menyebutkannya; jangan memakai nomor dari sumber lain.")
    if action in VERIFY_ACTIONS:
        now = time.time()
        v = st["verify"]
        while v and now - v[0] > 900:
            v.popleft()
        if len(v) >= VERIFY_PER_SESSION or not verify_ip_limiter.hit(ip):
            return err_json("TERLALU_BANYAK_PERCOBAAN",
                            "Terlalu banyak percobaan verifikasi. Arahkan pelanggan ke tim CS atau coba lagi nanti.")
        v.append(now)
    if action == "create_order":  # cegah pesanan ganda untuk permintaan identik
        key = _hash(json.dumps(clean, sort_keys=True, ensure_ascii=False))
        hit = st["orders"].get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        out = call_pos(clean)
        try:
            ok = "error" not in (json.loads(out) if out.lstrip().startswith("{") else {})
        except ValueError:
            ok = True
        if ok:
            st["orders"][key] = (time.time(), out)
        return out
    log.info("pos_api action=%s", action)
    return call_pos(clean)


# --- Streaming ----------------------------------------------------------------------------
_pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="pos")
_client = None
_client_lock = threading.Lock()
GENERIC_ERROR = "Maaf, layanan sedang bermasalah. Silakan coba lagi."


def get_client():
    global _client
    with _client_lock:
        if _client is None:
            base = AZURE_OPENAI_ENDPOINT.rstrip("/") + "/openai/v1/"
            _client = OpenAI(base_url=base, api_key=AZURE_OPENAI_API_KEY, timeout=120)
        return _client


def sse(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def event_stream(message, st, ip):
    """status, delta, done/error. Satu giliran bisa berisi beberapa putaran model + tool.

    ID percakapan baru disimpan ke state hanya bila giliran selesai utuh, supaya tidak tertinggal
    function_call tanpa hasil bila terjadi error di tengah jalan.
    """
    try:
        client = get_client()
        next_input = [{"role": "user", "content": message}]
        last_id = st["conv"]
        for _ in range(MAX_TOOL_ROUNDS):
            kwargs = dict(model=MODEL_DEPLOYMENT, instructions=INSTRUCTIONS, input=next_input,
                          tools=[POS_TOOL], stream=True)
            if last_id:
                kwargs["previous_response_id"] = last_id
            if REASONING_EFFORT:
                kwargs["reasoning"] = {"effort": REASONING_EFFORT}
            calls = []
            got_text = False
            stream = client.responses.create(**kwargs)
            try:
                for ev in stream:
                    kind = getattr(ev, "type", "")
                    if kind == "response.output_text.delta":
                        got_text = True
                        yield sse({"type": "delta", "text": ev.delta})
                    elif kind == "response.output_item.added" and not got_text:
                        t = getattr(getattr(ev, "item", None), "type", "")
                        if t == "function_call":
                            yield sse({"type": "status", "text": "Mengambil data toko…"})
                        elif t == "reasoning":
                            yield sse({"type": "status", "text": "Berpikir…"})
                    elif kind == "response.output_item.done":
                        item = getattr(ev, "item", None)
                        if getattr(item, "type", "") == "function_call":
                            calls.append(item)
                    elif kind == "response.completed":
                        last_id = ev.response.id
                    elif kind == "response.incomplete":
                        last_id = getattr(getattr(ev, "response", None), "id", last_id)
                        if not got_text and not calls:
                            log.error("Respons model tidak lengkap tanpa isi")
                            yield sse({"type": "error", "message": GENERIC_ERROR})
                            return
                        log.warning("Respons model terpotong (incomplete)")
                    elif kind in ("response.failed", "error"):
                        resp_err = getattr(getattr(ev, "response", None), "error", None)
                        log.error("Model gagal: type=%s code=%s", kind,
                                  getattr(resp_err, "code", None) or getattr(ev, "code", None))
                        yield sse({"type": "error", "message": GENERIC_ERROR})
                        return
            finally:  # juga jalan saat klien memutus koneksi (GeneratorExit): hentikan stream ke model
                try:
                    stream.close()
                except Exception:
                    pass
            if not calls:
                st["conv"] = last_id
                yield sse({"type": "done"})
                return
            next_input = []
            for c in calls:
                try:
                    args = json.loads(c.arguments or "{}")
                except ValueError:
                    args = {}
                fut = _pool.submit(run_tool, args, st, ip)
                while True:  # heartbeat supaya proxy tidak memutus koneksi saat POS lambat
                    try:
                        out = fut.result(timeout=10)
                        break
                    except FutureTimeout:
                        yield ": ping\n\n"
                next_input.append({"type": "function_call_output", "call_id": c.call_id, "output": out})
        yield sse({"type": "error", "message": "Maaf, permintaan terlalu rumit. Coba pertanyaan yang lebih singkat."})
    except Exception:
        log.exception("Gagal memanggil model")
        yield sse({"type": "error", "message": GENERIC_ERROR})
    finally:
        st["busy"] = False


# --- Keamanan HTTP ----------------------------------------------------------------------------
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")


@app.before_request
def origin_check():
    """Tolak POST lintas-origin (pertahanan CSRF tambahan di atas SameSite dan JSON-only)."""
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") not in ALLOWED_ORIGINS and urlparse(origin).netloc != request.host:
            return jsonify(error="Permintaan ditolak."), 403


@app.after_request
def security_headers(resp):
    h = resp.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.is_secure:
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path.startswith("/api/") or request.path == "/healthz":
        h["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="Permintaan terlalu besar."), 413


@app.errorhandler(404)
def not_found(_):
    return jsonify(error="Tidak ditemukan."), 404


# --- Route -----------------------------------------------------------------------------------------
@app.get("/")
def index():
    resp = send_from_directory(app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/healthz")
def healthz():
    info = {"status": "ok", "build": BUILD}
    token = request.headers.get("X-Health-Token", "")
    if HEALTH_TOKEN and hmac.compare_digest(token, HEALTH_TOKEN):
        info["config"] = dict(endpoint=bool(AZURE_OPENAI_ENDPOINT), api_key=bool(AZURE_OPENAI_API_KEY),
                              mcp_url=bool(MCP_SERVER_URL), mcp_key=bool(MCP_SUBSCRIPTION_KEY),
                              model=MODEL_DEPLOYMENT)
        if request.args.get("deep") == "1":  # uji nyata; hanya dengan token dan dibatasi 3x/menit
            if not deep_health_limiter.hit("deep"):
                return jsonify(error="Terlalu banyak permintaan."), 429
            try:
                r = _mcp_post({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, timeout=15)
                info["pos"] = "OK" if r.status_code == 200 else f"GAGAL (HTTP {r.status_code})"
            except Exception as e:
                info["pos"] = f"GAGAL ({type(e).__name__})"
            try:
                get_client().responses.create(model=MODEL_DEPLOYMENT, input="ping", max_output_tokens=16)
                info["model"] = "OK"
            except Exception as e:
                log.error("Health deep model gagal: %s", type(e).__name__)
                info["model"] = f"GAGAL ({type(e).__name__})"
    return jsonify(info)


@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return jsonify(error="Pesan tidak boleh kosong."), 400
    message = RE_CTRL.sub("", message).strip()
    if len(message) > MAX_MESSAGE_CHARS:
        return jsonify(error=f"Pesan terlalu panjang (maks {MAX_MESSAGE_CHARS} karakter)."), 400

    ip = client_ip()
    if not chat_limiter.hit(ip):
        return jsonify(error="Terlalu banyak pesan. Coba lagi sebentar lagi."), 429
    if not daily_budget_ok():
        return jsonify(error="Layanan sedang penuh hari ini. Silakan hubungi tim CS."), 429

    sid = session.get("sid")
    if not sid:
        sid = session["sid"] = uuid.uuid4().hex
    session.permanent = True
    st = get_state(sid)
    if st["busy"]:
        return jsonify(error="Mohon tunggu jawaban sebelumnya selesai."), 429
    if st["turns"] >= MAX_TURNS_PER_CONVERSATION:
        return jsonify(error="Percakapan sudah panjang. Klik “Percakapan baru” untuk melanjutkan."), 400

    st["busy"] = True
    st["turns"] += 1
    remember_identity(st, message)

    resp = Response(
        stream_with_context(event_stream(message, st, ip)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
    resp.call_on_close(lambda: st.__setitem__("busy", False))
    return resp


@app.post("/api/reset")
def reset():
    sid = session.pop("sid", None)
    if sid:
        drop_state(sid)
    return jsonify(ok=True)


if __name__ == "__main__":
    # Hanya untuk pengembangan lokal. Produksi: gunicorn lewat startup.sh.
    app.run(host=_env("HOST", "127.0.0.1"), port=_env_int("PORT", 8000))
