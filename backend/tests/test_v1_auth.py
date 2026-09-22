"""V1 多租户认证单元测试（不依赖外部服务）。

覆盖：
- JWT 签发/解析/过期识别
- bcrypt 密码哈希/校验（含错误密码、空密码）
- API Key 生成格式 + hash 一致性 + 校验
- TenantContext ContextVar set/reset 隔离
- register_tenant 的 email 重复检测（用 in-memory SQLite 模拟）

运行：pytest tests/test_v1_auth.py -v
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest

from app import auth
from app.auth import (
    TenantContext,
    authenticate,
    create_access_token,
    decode_token,
    generate_api_key,
    get_current_tenant,
    hash_password,
    register_tenant,
    reset_tenant_context,
    set_tenant_context,
    verify_password,
)


# ---------------------------------------------------------------------- #
# JWT
# ---------------------------------------------------------------------- #


def test_jwt_roundtrip():
    token = create_access_token(tenant_id="t1", user_id="u1", email="a@x.com")
    payload = decode_token(token)
    assert payload["tenant_id"] == "t1"
    assert payload["sub"] == "u1"
    assert payload["email"] == "a@x.com"
    assert "iat" in payload and "exp" in payload
    assert payload["exp"] > payload["iat"]


def test_jwt_invalid_token_raises():
    from jose import JWTError

    with pytest.raises(JWTError):
        decode_token("not.a.valid.jwt")


def test_jwt_tampered_payload_rejected():
    token = create_access_token(tenant_id="t1", user_id="u1", email="a@x.com")
    # 篡改 payload 段
    parts = token.split(".")
    tampered = parts[0] + "." + parts[1][:-3] + "xxx" + "." + parts[2]
    from jose import JWTError

    with pytest.raises(JWTError):
        decode_token(tampered)


# ---------------------------------------------------------------------- #
# bcrypt 密码
# ---------------------------------------------------------------------- #


def test_password_hash_and_verify():
    h = hash_password("secret12345")
    assert h != "secret12345"
    assert verify_password("secret12345", h) is True


def test_password_verify_wrong():
    h = hash_password("secret12345")
    assert verify_password("wrong", h) is False
    assert verify_password("", h) is False
    assert verify_password("SECRET12345", h) is False  # 大小写敏感


def test_password_hash_unique_per_call():
    h1 = hash_password("samepass123")
    h2 = hash_password("samepass123")
    assert h1 != h2  # salt 随机
    assert verify_password("samepass123", h1) and verify_password("samepass123", h2)


def test_password_truncated_to_72_bytes():
    # bcrypt 上限 72 字节；超长不应抛异常
    long_pw = "x" * 200
    h = hash_password(long_pw)
    # 200 字符 ASCII = 200 字节，截断到 72 后哈希；校验时也截断 → 一致
    assert verify_password(long_pw, h) is True


# ---------------------------------------------------------------------- #
# API Key
# ---------------------------------------------------------------------- #


def test_api_key_format():
    k = generate_api_key()
    assert k.startswith("af-")
    # af- + 32 hex = 35 字符
    assert len(k) == 35
    # hex 部分合法
    assert all(c in "0123456789abcdef" for c in k[3:])


def test_api_key_unique():
    keys = {generate_api_key() for _ in range(100)}
    assert len(keys) == 100


def test_api_key_hash_internal():
    # 内部 hash 函数应是稳定的（同输入同输出）
    import hashlib

    from app.auth import _hash_api_key

    k = "af-" + "a" * 32
    h1 = _hash_api_key(k)
    h2 = _hash_api_key(k)
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert h1 == "sha256:" + hashlib.sha256(k.encode()).hexdigest()


# ---------------------------------------------------------------------- #
# ContextVar 隔离
# ---------------------------------------------------------------------- #


def test_contextvar_default_none():
    assert get_current_tenant() is None


def test_contextvar_set_and_reset():
    ctx = TenantContext(tenant_id="t1", user_id="u1", email="a@x.com")
    token = set_tenant_context(ctx)
    try:
        assert get_current_tenant() is ctx
        assert get_current_tenant().tenant_id == "t1"
    finally:
        reset_tenant_context(token)
    assert get_current_tenant() is None


def test_contextvar_independent_per_task():
    """asyncio 任务间 ContextVar 不串扰。"""
    import asyncio

    seen: list[str | None] = []

    async def worker(tenant_id: str | None, delay: float):
        if tenant_id:
            token = set_tenant_context(
                TenantContext(tenant_id=tenant_id, user_id="u", email="e")
            )
        try:
            await asyncio.sleep(delay)
            seen.append((tenant_id, get_current_tenant().tenant_id if get_current_tenant() else None))
        finally:
            if tenant_id:
                reset_tenant_context(token)

    async def run():
        await asyncio.gather(
            worker("tA", 0.02),
            worker(None, 0.01),
            worker("tB", 0.03),
        )

    asyncio.run(run())
    # 每个任务看到的都是自己设置的（或 None）
    for tid, seen_tid in seen:
        assert tid == seen_tid, (tid, seen_tid)


# ---------------------------------------------------------------------- #
# register_tenant / authenticate（用 in-memory SQLite 模拟）
# ---------------------------------------------------------------------- #


@asynccontextmanager
async def _sqlite_engine():
    """用 aiosqlite 起一个内存 SQLite，临时替换全局 engine。

    若环境没装 aiosqlite，则跳过此类集成测试。
    """
    pytest.importorskip("aiosqlite")
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    import app.models as models

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    # 临时替换全局
    old_engine, old_sm = models._engine, models._sessionmaker
    models._engine = engine
    models._sessionmaker = sm
    try:
        yield sm
    finally:
        await engine.dispose()
        models._engine = old_engine
        models._sessionmaker = old_sm


def test_register_tenant_returns_api_key_once():
    async def run():
        async with _sqlite_engine():
            result = await register_tenant("Acme", "alice@acme.com", "passw0rd!")
            assert result["tenant_id"]
            assert result["user_id"]
            assert result["email"] == "alice@acme.com"
            assert result["api_key"].startswith("af-")
            return result

    asyncio.run(run())


def test_register_tenant_duplicate_email_409():
    async def run():
        async with _sqlite_engine():
            await register_tenant("Acme", "alice@acme.com", "passw0rd!")
            with pytest.raises(Exception) as exc:
                await register_tenant("Other", "alice@acme.com", "passw0rd!")
            # FastAPI HTTPException 的 status_code == 409
            assert getattr(exc.value, "status_code", None) == 409

    asyncio.run(run())


def test_authenticate_success_and_failure():
    async def run():
        async with _sqlite_engine():
            await register_tenant("Acme", "bob@acme.com", "passw0rd!")
            ctx, token = await authenticate("bob@acme.com", "passw0rd!")
            assert ctx.tenant_id and ctx.user_id and ctx.email == "bob@acme.com"
            assert token
            # 错误密码
            with pytest.raises(Exception) as exc:
                await authenticate("bob@acme.com", "wrong")
            assert getattr(exc.value, "status_code", None) == 401
            # 不存在的用户
            with pytest.raises(Exception) as exc:
                await authenticate("nobody@acme.com", "x")
            assert getattr(exc.value, "status_code", None) == 401

    asyncio.run(run())


def test_resolve_api_key_finds_tenant():
    async def run():
        async with _sqlite_engine():
            result = await register_tenant("Acme", "carol@acme.com", "passw0rd!")
            ctx = await auth.resolve_api_key(result["api_key"])
            assert ctx is not None
            assert ctx.tenant_id == result["tenant_id"]
        # 错误的 key
            assert await auth.resolve_api_key("af-deadbeef") is None
            assert await auth.resolve_api_key("not-an-af-key") is None

    asyncio.run(run())
