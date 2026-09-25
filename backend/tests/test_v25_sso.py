"""V3-T8：SSO（OAuth2 授权码最小子集）测试。

不依赖真实 IdP / 网络。覆盖：
- Stub IdP：授权码 → token → userinfo 全流程 + transport 接口
- build_authorization_url 参数 / encode_state / decode_state（过期/篡改）
- normalize_userinfo 各 provider 映射
- sso_login（SQLite）：新建绑定用户 / 复用既有用户 / tenant 缺失
- sso_providers_from_env 环境变量发现（monkeypatch）
- 演示登录：transport=StubIdP 走完整授权码流
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

import app.models as models
from app.sso import (
    DEFAULT_SCOPES,
    SSOError,
    SSOExchangeError,
    SSOStateError,
    SSOUserInfoError,
    STUB_PROVIDER,
    StubIdP,
    build_authorization_url,
    decode_state,
    encode_state,
    normalize_userinfo,
    sso_login,
    sso_providers_from_env,
    stub_provider_config,
)

try:
    pytest.importorskip("aiosqlite")
except Exception:  # pragma: no cover
    pass


@asynccontextmanager
async def _sqlite():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    old_engine, old_sm = models._engine, models._sessionmaker
    models._engine = engine
    models._sessionmaker = sm
    try:
        yield sm
    finally:
        await engine.dispose()
        models._engine = old_engine
        models._sessionmaker = old_sm


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------- #
# Stub IdP
# ---------------------------------------------------------------------- #


def test_sso_account_model_tables():
    names = set(models.Base.metadata.tables.keys())
    assert "sso_accounts" in names
    tpl = models.Base.metadata.tables["sso_accounts"]
    cols = set(tpl.columns.keys())
    assert {
        "id",
        "tenant_id",
        "user_id",
        "provider",
        "subject",
        "email",
    } <= cols
    idx = {i.name for i in tpl.indexes}
    assert "ix_sso_accounts_provider_subject" in idx
    assert "ix_sso_accounts_tenant" in idx


def test_stub_idp_full_flow():
    idp = StubIdP()
    idp.register_user("alice@example.com", "Alice")
    code = idp.authorize("alice@example.com")
    token = _run(idp.exchange_code(code))
    info = _run(idp.userinfo(token))
    assert info["email"] == "alice@example.com"
    assert info["name"] == "Alice"
    assert info["sub"].startswith("sub_")


def test_stub_idp_invalid_code_and_token():
    idp = StubIdP()
    idp.register_user("a@x.com")
    with pytest.raises(SSOExchangeError):
        _run(idp.exchange_code("bad-code"))
    with pytest.raises(SSOUserInfoError):
        _run(idp.userinfo("bad-token"))
    # 授权码一次性
    code = idp.authorize("a@x.com")
    _run(idp.exchange_code(code))
    with pytest.raises(SSOExchangeError):
        _run(idp.exchange_code(code))


def test_stub_idp_unauthorized_user():
    idp = StubIdP()
    with pytest.raises(SSOError):
        idp.authorize("ghost@x.com")


def test_stub_idp_transport_interface():
    idp = StubIdP()
    idp.register_user("b@x.com")
    code = idp.authorize("b@x.com")
    token_resp = _run(idp.post("https://idp.stub.local/token", {"code": code}))
    info = _run(
        idp.get(
            "https://idp.stub.local/userinfo",
            {"Authorization": f"Bearer {token_resp['access_token']}"},
        )
    )
    assert info["email"] == "b@x.com"


# ---------------------------------------------------------------------- #
# 授权 URL / state
# ---------------------------------------------------------------------- #


def test_build_authorization_url():
    cfg = stub_provider_config()
    url = build_authorization_url(cfg, state="st", redirect_uri="http://cb")
    assert "response_type=code" in url
    assert "client_id=stub-client" in url
    assert "redirect_uri=http%3A%2F%2Fcb" in url
    assert "state=st" in url
    assert "scope=" in url
    assert cfg.scopes == DEFAULT_SCOPES


def test_state_roundtrip():
    state = encode_state(tenant_id="t1", redirect_uri="http://cb", provider="stub")
    payload = decode_state(state, provider="stub")
    assert payload["tenant_id"] == "t1"
    assert payload["redirect_uri"] == "http://cb"
    assert payload["provider"] == "stub"


def test_state_wrong_provider_and_bad():
    state = encode_state(tenant_id="t1", redirect_uri="u", provider="stub")
    with pytest.raises(SSOStateError):
        decode_state(state, provider="google")
    with pytest.raises(SSOStateError):
        decode_state("not-base64!!", provider="stub")


def test_state_expired():
    state = encode_state(tenant_id="t1", redirect_uri="u", provider="stub")
    payload = json.loads(__import__("base64").urlsafe_b64decode(state.encode()).decode())
    payload["exp"] = 0
    import base64

    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode("ascii")
    with pytest.raises(SSOStateError):
        decode_state(raw, provider="stub")


# ---------------------------------------------------------------------- #
# normalize_userinfo
# ---------------------------------------------------------------------- #


def test_normalize_userinfo_providers():
    assert normalize_userinfo("google", {"sub": "g1", "email": "A@X.com", "name": "Ann"}) == {
        "subject": "g1",
        "email": "a@x.com",
        "display_name": "Ann",
    }
    assert normalize_userinfo("github", {"id": 123, "email": "git@x.com", "login": "git"}) == {
        "subject": "123",
        "email": "git@x.com",
        "display_name": "git",
    }
    # 未知 provider 回退 google 映射
    assert normalize_userinfo("generic", {"sub": "s", "email": "e@x.com"})["subject"] == "s"


def test_normalize_userinfo_missing_subject():
    with pytest.raises(SSOUserInfoError):
        normalize_userinfo("google", {})


# ---------------------------------------------------------------------- #
# sso_login（SQLite 全流程）
# ---------------------------------------------------------------------- #


async def _seed_tenant(sm, tenant_id="t1"):
    async with sm() as s:
        async with s.begin():
            s.add(models.Tenant(id=tenant_id, name="A", api_key_hash="h"))


def test_sso_login_new_user_bound():
    async def run():
        async with _sqlite() as sm:
            await _seed_tenant(sm)
            cfg = stub_provider_config()
            idp = StubIdP()
            idp.register_user("alice@example.com", "Alice")
            code = idp.authorize("alice@example.com")
            result = await sso_login(
                cfg,
                code,
                redirect_uri=None,
                tenant_id="t1",
                session_factory=sm,
                transport=idp,
            )
            assert result["tenant_id"] == "t1"
            assert result["email"] == "alice@example.com"
            assert result["bound"] is True
            assert result["access_token"]
            # 绑定已落库
            async with sm() as s:
                from sqlalchemy import select

                acc = (
                    await s.execute(select(models.SSOAccount))
                ).scalars().first()
                assert acc is not None
                assert acc.provider == STUB_PROVIDER
                user = (
                    await s.execute(
                        select(models.User).where(models.User.id == acc.user_id)
                    )
                ).scalar_one()
                assert user.password_hash == "!"
            return result

    result = _run(run())
    assert result["email"] == "alice@example.com"


def test_sso_login_reuses_existing_user():
    async def run():
        async with _sqlite() as sm:
            await _seed_tenant(sm)
            cfg = stub_provider_config()
            idp = StubIdP()
            idp.register_user("alice@example.com", "Alice")
            code = idp.authorize("alice@example.com")
            r1 = await sso_login(
                cfg, code, None, tenant_id="t1", session_factory=sm, transport=idp
            )
            # 再次登录：复用绑定（不新建）
            code2 = idp.authorize("alice@example.com")
            r2 = await sso_login(
                cfg, code2, None, tenant_id="t1", session_factory=sm, transport=idp
            )
            async with sm() as s:
                from sqlalchemy import select

                accs = (
                    await s.execute(select(models.SSOAccount))
                ).scalars().all()
                assert len(accs) == 1
            assert r2["bound"] is False
            assert r2["user_id"] == r1["user_id"]
            return r2

    _run(run())


def test_sso_login_tenant_not_found():
    async def run():
        async with _sqlite() as sm:
            cfg = stub_provider_config()
            idp = StubIdP()
            idp.register_user("a@x.com")
            code = idp.authorize("a@x.com")
            with pytest.raises(Exception) as exc_info:
                await sso_login(
                    cfg, code, None, tenant_id="nope", session_factory=sm, transport=idp
                )
            return exc_info.value

    exc = _run(run())
    assert getattr(exc, "status_code", None) == 404


def test_sso_providers_from_env(monkeypatch):
    monkeypatch.delenv("SSO_STUB_ENABLED", raising=False)
    provs = sso_providers_from_env()
    assert STUB_PROVIDER in provs
    # 配置真实 provider
    monkeypatch.setenv("SSO_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("SSO_GOOGLE_CLIENT_SECRET", "cs")
    monkeypatch.setenv("SSO_GOOGLE_AUTHORIZATION_URL", "https://accounts.google.com/o/oauth2/v2/auth")
    monkeypatch.setenv("SSO_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
    monkeypatch.setenv("SSO_GOOGLE_USERINFO_URL", "https://openidconnect.googleapis.com/v1/userinfo")
    provs = sso_providers_from_env()
    assert "google" in provs
    assert provs["google"].client_id == "cid"


def test_sso_demo_login_via_transport():
    """demo-login 路径：StubIdP 作为 transport 走完整授权码流。"""
    async def run():
        async with _sqlite() as sm:
            await _seed_tenant(sm)
            cfg = stub_provider_config()
            idp = StubIdP()
            idp.register_user("demo@x.com", "Demo")
            code = idp.authorize("demo@x.com")
            result = await sso_login(
                cfg,
                code,
                None,
                tenant_id="t1",
                session_factory=sm,
                transport=idp,
            )
            return result

    result = _run(run())
    assert result["access_token"]
    assert result["email"] == "demo@x.com"
