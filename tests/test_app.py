import importlib
import json
import os
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load_app(monkeypatch, **env):
    base = dict(APP_ENV="dev", SECRET_KEY="test-secret", COOKIE_SECURE="0", TRUSTED_PROXIES="0",
                AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com/", AZURE_OPENAI_API_KEY="k",
                MCP_SERVER_URL="https://pos.example/mcp", MCP_SUBSCRIPTION_KEY="m",
                RATE_LIMIT_PER_MIN="1000", HEALTH_TOKEN="tok")
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("app", None)
    return importlib.import_module("app")


@pytest.fixture
def mod(monkeypatch):
    m = load_app(monkeypatch)
    m.app.config["TESTING"] = True
    return m


# ---------- helper: model palsu ----------
def ev(type_, **kw):
    return NS(type=type_, **kw)


class FakeStream(list):
    closed = False

    def close(self):
        self.closed = True


def text_round(text, rid="resp_1"):
    return FakeStream([ev("response.output_text.delta", delta=text),
                       ev("response.completed", response=NS(id=rid))])


def call_round(args, rid="resp_c", call_id="c1"):
    item = NS(type="function_call", arguments=json.dumps(args), call_id=call_id)
    return FakeStream([ev("response.output_item.done", item=item),
                       ev("response.completed", response=NS(id=rid))])


def fake_client(rounds, seen=None):
    it = iter(rounds)

    def create(**kw):
        if seen is not None:
            seen.append(kw)
        return next(it)

    return NS(responses=NS(create=create))


def parse_sse(data):
    return [json.loads(c[6:]) for c in data.decode().split("\n\n") if c.startswith("data: ")]


def chat(client, msg):
    return client.post("/api/chat", json={"message": msg})


# ---------- validasi ----------
def test_validate_ok_and_bad(mod):
    c, e = mod.validate_call({"action": "get_order", "order_id": "TK-1042", "phone_last4": "1234", "junk": "x"})
    assert e is None and c == {"action": "get_order", "order_id": "TK-1042", "phone_last4": "1234"}
    for bad in [
        {"action": "nope"},
        {"action": "get_order", "order_id": "../etc", "phone_last4": "1234"},
        {"action": "get_order", "order_id": "TK-1", "phone_last4": "12a4"},
        {"action": "create_order", "sku": "A-1", "qty": -3, "customer_name": "Budi", "phone": "081234567890",
         "address": "Jl. Mawar No 1 Jakarta 12345", "email": "b@x.com"},
        {"action": "create_order", "sku": "A-1", "qty": 9999, "customer_name": "Budi", "phone": "081234567890",
         "address": "Jl. Mawar No 1 Jakarta 12345", "email": "b@x.com"},
        {"action": "compare_products", "skus": "ONLYONE"},
    ]:
        assert mod.validate_call(bad)[0] is None, bad


def test_invalid_email_code(mod):
    _, e = mod.validate_call({"action": "create_order", "sku": "A-1", "qty": 1, "customer_name": "Budi",
                              "phone": "081234567890", "address": "Jl. Mawar No 1 Jakarta 12345",
                              "email": "budi@gmail"})
    assert "INVALID_EMAIL" in e


# ---------- identitas ----------
def test_identity_must_be_typed_by_customer(mod):
    st = mod.get_state("s1")
    mod.remember_identity(st, "pesanan TK-1042, hp saya 0812-3456-7890")
    ok = {"action": "list_orders", "phone": "+62 812 3456 7890", "customer_name": "Budi"}
    assert mod.identity_ok(st, ok)  # 08.. dan +62.. dianggap sama
    assert mod.identity_ok(st, {"phone_last4": "7890"})
    assert not mod.identity_ok(st, {"phone": "081299998888"})
    assert not mod.identity_ok(st, {"phone_last4": "1042"})  # angka dari nomor pesanan bukan identitas


def test_run_tool_blocks_foreign_phone_without_calling_pos(mod, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "call_pos", lambda c: calls.append(c) or "{}")
    st = mod.get_state("s2")
    out = mod.run_tool({"action": "list_orders", "phone": "081299998888", "customer_name": "Budi"}, st, "1.1.1.1")
    assert "VERIFIKASI_DITOLAK" in out and not calls


def test_verify_attempts_limited(mod, monkeypatch):
    monkeypatch.setattr(mod, "call_pos", lambda c: "{}")
    st = mod.get_state("s3")
    mod.remember_identity(st, "7890")
    outs = [mod.run_tool({"action": "get_order", "order_id": "TK-1", "phone_last4": "7890"}, st, "9.9.9.9")
            for _ in range(mod.VERIFY_PER_SESSION + 2)]
    assert "TERLALU_BANYAK_PERCOBAAN" in outs[-1] and "TERLALU_BANYAK" not in outs[0]


def test_create_order_idempotent(mod, monkeypatch):
    n = []
    monkeypatch.setattr(mod, "call_pos", lambda c: n.append(1) or json.dumps({"order_id": "TK-9"}))
    st = mod.get_state("s4")
    mod.remember_identity(st, "081234567890")
    args = {"action": "create_order", "sku": "A-1", "qty": 1, "customer_name": "Budi", "phone": "081234567890",
            "address": "Jl. Mawar No 1 Jakarta 12345", "email": "b@x.com"}
    assert mod.run_tool(args, st, "1.1.1.1") == mod.run_tool(args, st, "1.1.1.1")
    assert len(n) == 1


# ---------- HTTP ----------
def test_headers_and_health(mod):
    c = mod.app.test_client()
    r = c.get("/")
    assert "script-src 'self'" in r.headers["Content-Security-Policy"] and r.headers["X-Frame-Options"] == "DENY"
    r = c.get("/healthz?deep=1")
    assert r.get_json() == {"status": "ok", "build": mod.BUILD}  # tanpa token: tidak ada detail
    assert "config" in c.get("/healthz", headers={"X-Health-Token": "tok"}).get_json()
    assert "config" not in c.get("/healthz", headers={"X-Health-Token": "salah"}).get_json()


def test_input_limits_and_origin(mod):
    c = mod.app.test_client()
    assert chat(c, "").status_code == 400
    assert chat(c, "x" * (mod.MAX_MESSAGE_CHARS + 1)).status_code == 400
    r = c.post("/api/chat", json={"message": "hai"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert c.post("/api/chat", data="x" * 20000, content_type="application/json").status_code == 413


def test_client_cannot_inject_conversation_id(mod, monkeypatch):
    seen = []
    monkeypatch.setattr(mod, "get_client", lambda: fake_client([text_round("Halo")], seen))
    c = mod.app.test_client()
    r = c.post("/api/chat", json={"message": "hai", "conversation_id": "resp_orang_lain"})
    parse_sse(r.data)
    assert "previous_response_id" not in seen[0]


def test_stream_and_conversation_chaining(mod, monkeypatch):
    seen = []
    s1 = text_round("Halo", "resp_A")
    monkeypatch.setattr(mod, "get_client", lambda: fake_client([s1, text_round("Lagi", "resp_B")], seen))
    c = mod.app.test_client()
    evs = parse_sse(chat(c, "hai").data)
    assert [e["type"] for e in evs] == ["delta", "done"] and s1.closed
    parse_sse(chat(c, "lagi").data)
    assert seen[1]["previous_response_id"] == "resp_A"
    # sesi lain tidak mewarisi percakapan
    other = mod.app.test_client()
    monkeypatch.setattr(mod, "get_client", lambda: fake_client([text_round("x")], seen))
    parse_sse(chat(other, "hai").data)
    assert "previous_response_id" not in seen[2]


def test_tool_round_uses_validation(mod, monkeypatch):
    posted = []
    monkeypatch.setattr(mod, "call_pos", lambda c: posted.append(c) or json.dumps({"items": []}))
    rounds = [call_round({"action": "search_product", "query": "blender"}), text_round("Ini hasilnya", "resp_2")]
    seen = []
    monkeypatch.setattr(mod, "get_client", lambda: fake_client(rounds, seen))
    c = mod.app.test_client()
    evs = parse_sse(chat(c, "ada blender?").data)
    assert evs[-1]["type"] == "done" and posted == [{"action": "search_product", "query": "blender"}]
    assert seen[1]["input"][0]["type"] == "function_call_output"


def test_error_mid_turn_does_not_commit_conversation(mod, monkeypatch):
    bad = FakeStream([ev("response.failed", response=NS(error=NS(code="server_error")))])
    monkeypatch.setattr(mod, "get_client", lambda: fake_client([bad]))
    c = mod.app.test_client()
    evs = parse_sse(chat(c, "hai").data)
    assert evs[-1]["type"] == "error" and "server_error" not in json.dumps(evs)
    with c.session_transaction() as s:
        assert mod.get_state(s["sid"])["conv"] is None


def test_rate_limit_and_busy(monkeypatch):
    m = load_app(monkeypatch, RATE_LIMIT_PER_MIN="2")
    monkeypatch.setattr(m, "get_client", lambda: fake_client([text_round("a"), text_round("b"), text_round("c")]))
    c = m.app.test_client()
    codes = []
    for _ in range(3):
        r = chat(c, "hai")
        r.get_data()
        codes.append(r.status_code)
    assert codes == [200, 200, 429]


def test_reset_clears_state(mod, monkeypatch):
    monkeypatch.setattr(mod, "get_client", lambda: fake_client([text_round("a", "resp_X"), text_round("b")]))
    c = mod.app.test_client()
    parse_sse(chat(c, "hai").data)
    assert c.post("/api/reset").get_json() == {"ok": True}
    seen = []
    monkeypatch.setattr(mod, "get_client", lambda: fake_client([text_round("c")], seen))
    parse_sse(chat(c, "baru").data)
    assert "previous_response_id" not in seen[0]


def test_secret_key_required_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    sys.modules.pop("app", None)
    with pytest.raises(RuntimeError):
        importlib.import_module("app")
