"""AI Coding upstream client error forwarding."""
from __future__ import annotations

from src.dashboard.ai_coding_client import fetch_upstream_method


def test_fetch_upstream_method_preserves_non_json_error_status(monkeypatch) -> None:
    import io
    import urllib.error

    def fake_urlopen(_request, timeout=0):
        raise urllib.error.HTTPError(
            url="http://test/api",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=io.BytesIO(b"Internal Server Error"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    status, payload = fetch_upstream_method(
        "POST",
        "/api/v1/execution-programs/x/phases/y/continue",
        json_body={"actor": "operator"},
    )
    assert status == 500
    assert payload["detail"] == "Internal Server Error"
