"""Wave B — Login brute-force protection + media upload hardening."""
from __future__ import annotations

import io
import os
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    from src.dashboard import security_hardening as sh

    with sh._login_lock:
        sh._login_failures_by_identity.clear()
        sh._login_failures_by_ip.clear()
    yield
    with sh._login_lock:
        sh._login_failures_by_identity.clear()
        sh._login_failures_by_ip.clear()


@pytest.fixture()
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_ADMIN_TOKEN", "wave-b-test-token-not-for-prod")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SCHEDULER_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("MESSAGES_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("SCHEDULED_DM_ENABLED", "false")
    monkeypatch.setenv("READINESS_WORKER_ENABLED", "false")
    monkeypatch.setenv("AI_AGENT_AUTO_LOOP_ENABLED", "false")
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setenv("MEDIA_DIR", str(media))

    from src.dashboard.app import create_app

    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def _auth_headers():
    return {"X-Admin-Token": "wave-b-test-token-not-for-prod"}


def test_wave_b_source_contracts():
    auth = (ROOT / "src/dashboard/auth_routes.py").read_text(encoding="utf-8")
    assert "login_rate_limited" in auth
    assert "record_login_failure" in auth
    assert "dashboard_login_failed" in auth
    # Never log password values
    assert "password=" not in auth
    assert "password}," not in auth

    hard = (ROOT / "src/dashboard/security_hardening.py").read_text(encoding="utf-8")
    assert "MEDIA_MAX_BYTES" in hard
    assert "sniff_media_kind" in hard

    routes = (ROOT / "src/dashboard/routes.py").read_text(encoding="utf-8")
    assert "sniff_media_kind" in routes
    assert "MEDIA_MAX_BYTES" in routes


def test_login_rate_limit_blocks_after_failures(client, monkeypatch):
    from src.dashboard import security_hardening as sh

    monkeypatch.setattr(sh, "LOGIN_MAX_FAILURES_PER_IDENTITY", 3)

    for _ in range(3):
        r = client.post(
            "/login",
            data={"username": "nobody", "password": "wrong", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 200
        assert b"Invalid username or password" in r.data

    r = client.post(
        "/login",
        data={"username": "nobody", "password": "wrong", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 429
    assert b"Too many failed login attempts" in r.data


def test_sniff_rejects_exe_as_jpg(tmp_path):
    from src.dashboard.security_hardening import sniff_media_kind

    p = tmp_path / "evil.jpg"
    p.write_bytes(b"MZ\x90\x00" + b"\x00" * 20)
    ok, kind, err = sniff_media_kind(p, ".jpg")
    assert ok is False
    assert "JPEG" in err


def test_sniff_accepts_jpeg(tmp_path):
    from src.dashboard.security_hardening import sniff_media_kind

    p = tmp_path / "ok.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)
    ok, kind, err = sniff_media_kind(p, ".jpg")
    assert ok is True
    assert kind == "photo"
    assert err == ""


def test_upload_rejects_bad_mime(client, tmp_path, monkeypatch):
    from config.settings import settings

    media = tmp_path / "media2"
    media.mkdir()
    monkeypatch.setattr(settings.storage, "media_dir", str(media))

    data = {
        "file": (io.BytesIO(b"not-an-image-payload-xxxxx"), "spoof.jpg"),
    }
    r = client.post(
        "/api/media/upload",
        data=data,
        content_type="multipart/form-data",
        headers=_auth_headers(),
    )
    assert r.status_code == 400
    body = r.get_json() or {}
    assert "JPEG" in (body.get("error") or "")
    assert list(media.glob("*")) == []


def test_upload_accepts_valid_jpeg(client, tmp_path, monkeypatch):
    from config.settings import settings

    media = tmp_path / "media3"
    media.mkdir()
    monkeypatch.setattr(settings.storage, "media_dir", str(media))

    payload = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    data = {"file": (io.BytesIO(payload), "photo.jpg")}
    r = client.post(
        "/api/media/upload",
        data=data,
        content_type="multipart/form-data",
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    body = r.get_json() or {}
    assert body.get("success") is True
    assert body.get("type") == "photo"
    assert body.get("filename", "").startswith("upload_")
    assert body.get("filename", "").endswith(".jpg")
    assert (media / body["filename"]).is_file()


def test_upload_rejects_disallowed_extension(client):
    data = {"file": (io.BytesIO(b"#!/bin/sh\necho hi\n"), "x.sh")}
    r = client.post(
        "/api/media/upload",
        data=data,
        content_type="multipart/form-data",
        headers=_auth_headers(),
    )
    assert r.status_code == 400
    assert "not allowed" in (r.get_json() or {}).get("error", "").lower()


def test_anonymous_upload_still_401(client):
    data = {"file": (io.BytesIO(b"\xff\xd8\xff\xe0" + b"\x00" * 20), "a.jpg")}
    r = client.post(
        "/api/media/upload",
        data=data,
        content_type="multipart/form-data",
    )
    assert r.status_code == 401


def test_session_cookie_flags_configured(app):
    assert app.config.get("SESSION_COOKIE_HTTPONLY") is True
    assert app.config.get("SESSION_COOKIE_SAMESITE") == "Lax"
    assert app.config.get("REMEMBER_COOKIE_HTTPONLY") is True
    assert app.config.get("MAX_CONTENT_LENGTH") == 52 * 1024 * 1024
