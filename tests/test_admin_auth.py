import base64
import dataclasses

from fastapi.testclient import TestClient

import app.admin.server as server
from app.admin.server import LoginGuard


class FakeRepo:
    async def status(self):
        return {"online": False, "bot": None, "events": []}


def client(monkeypatch, password="s3cret-pw"):
    monkeypatch.setattr(server, "settings", dataclasses.replace(server.settings, admin_password=password,
                                                                admin_user="admin"))
    return TestClient(server.create_app(repo=FakeRepo()))


def auth(pw):
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}


def test_login_and_security_headers(monkeypatch):
    with client(monkeypatch) as c:
        assert c.get("/api/status").status_code == 401           # 첫 요청(자격 증명 없음)
        r = c.get("/api/status", headers=auth("s3cret-pw"))
        assert r.status_code == 200
        assert r.headers["X-Frame-Options"] == "DENY" and r.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_bruteforce_lockout(monkeypatch):
    with client(monkeypatch) as c:
        h = {"X-Forwarded-For": "203.0.113.9"}
        for _ in range(10):
            assert c.get("/api/status", headers={**h, **auth("wrong")}).status_code == 401
        # 잠긴 뒤에는 맞는 비밀번호도 거절
        assert c.get("/api/status", headers={**h, **auth("s3cret-pw")}).status_code == 429
        # 다른 IP 는 영향 없음
        assert c.get("/api/status", headers={"X-Forwarded-For": "198.51.100.7", **auth("s3cret-pw")}).status_code == 200


def test_tunnel_without_password_is_refused(monkeypatch):
    with client(monkeypatch, password=None) as c:
        assert c.get("/api/status").status_code == 200                          # 로컬은 열림(기존 동작)
        assert c.get("/api/status", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403


def test_guard_window():
    g = LoginGuard(per_ip=3, window=600, ip_lock=900)
    t = [1000.0]
    g._now = lambda: t[0]
    for _ in range(3):
        g.fail("a")
    assert g.locked("a") > 0
    t[0] += 901
    assert g.locked("a") == 0
