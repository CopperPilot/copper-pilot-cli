from __future__ import annotations

import json
import os
import stat

import httpx
import pytest

from copper_pilot_cli.copper_auth import (
    AuthenticationError,
    DeviceCredential,
    DeviceLogin,
    build_fingerprint,
    load_credential,
    normalize_flow_token,
    save_credential,
)


def test_private_credential_round_trip(tmp_path) -> None:
    target = tmp_path / ".state" / "auth.json"
    credential = DeviceCredential(
        api_key="cf_live_test",
        fingerprint="host-fingerprint",
        base_url="https://copperpilot.ai",
    )
    assert save_credential(credential, target) == ()
    assert load_credential(target) == credential
    assert json.loads(target.read_text())["version"] == 1
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("value", ["", None, "undefined", "white space", "x"])
def test_invalid_flow_tokens_are_rejected(value) -> None:
    with pytest.raises(AuthenticationError):
        normalize_flow_token(value)


def test_fingerprint_does_not_expose_platform_details() -> None:
    fingerprint = build_fingerprint()
    hostname, digest = fingerprint.rsplit("_", 1)
    assert hostname
    assert len(digest) == 64
    int(digest, 16)


@pytest.mark.asyncio
async def test_device_login_polls_until_ready(monkeypatch, tmp_path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "POST":
            return httpx.Response(200, json={"flow_token": "valid-token", "expires_in": 3})
        calls += 1
        if calls == 1:
            return httpx.Response(409, json={"retry_after_ms": 1})
        return httpx.Response(200, json={"status": "ready", "api_key": "cf_live_ready"})

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(
        "copper_pilot_cli.copper_auth.save_credential",
        lambda credential: (),
    )
    credential = await DeviceLogin("https://example.test").login(open_browser=False)
    assert credential.api_key == "cf_live_ready"
    assert calls == 2
