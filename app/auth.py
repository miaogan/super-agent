"""V1 多租户认证：JWT + ContextVar + 中间件 + service。

设计要点
--------
- **租户上下文**：用 ``ContextVar`` 保存当前请求的 (tenant_id, user_id)，
  供业务层随时取用，无需逐层透传。
- **密码哈希**：bcrypt（passlib），不存明文。
- **API Key**：租户注册时生成 ``af-<random32>`` 形式的明文 API Key，
  存 hash 用于校验；明文仅返回给租户一次。
- **JWT**：HS256，载荷含 ``tenant_id`` / ``user_id`` / ``email``，
  过期由 ``JWT_EXPIRE_MINUTES`` 控制（默认 24h）。
- **认证流程**：
  - 注册租户 → 拿到 tenant_id + api_key（明文，仅一次）
  - 注册用户 / 登录 → 拿到 JWT
  - 后续请求带 ``Authorization: Bearer <jwt>``，由中间件解析注入 ContextVar
"""

from __future__ import annotations

import contextvars
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Tenant,
    User,
    get_session_factory,
    get_tenant_by_api_key_hash,
    get_user_by_email,
)

# 直接调用 bcrypt（避免 passlib 1.7 + bcrypt 4.x 的 __about__ 兼容问题）
import bcrypt as _bcrypt

_bearer_scheme = HTTPBearer(auto_error=False)

# ContextVar：每请求独立，避免协程串租户
_tenant_ctx: contextvars.ContextVar["TenantContext | None"] = contextvars.ContextVar(
    "tenant_ctx", default=None
)


@dataclass(frozen=True)
class TenantContext:
    """当前请求的租户上下文（不可变）。"""

    tenant_id: str
    user_id: str
    email: str
    display_name: str | None = None


# ---------------------------------------------------------------------- #
# 密码 / API Key 工具
# ---------------------------------------------------------------------- #


def hash_password(password: str) -> str:
    """bcrypt 哈希（限制 72 字节，与 OpenBSD bcrypt 上限一致）。"""
    pw = password.encode("utf-8")[:72]
    return _bcrypt.hashpw(pw, _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def generate_api_key() -> str:
    """生成明文 API Key（``af-<32hex>``）。租户注册时返回，DB 只存 hash。"""
    return "af-" + secrets.token_hex(16)


def _hash_api_key(api_key: str) -> str:
    """API Key 的 hash（用 sha256，比 bcrypt 快；key 已是高熵无需慢哈希）。"""
    import hashlib

    return "sha256:" + hashlib.sha256(api_key.encode()).hexdigest()


# ---------------------------------------------------------------------- #
# JWT
# ---------------------------------------------------------------------- #


def create_access_token(*, tenant_id: str, user_id: str, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


# ---------------------------------------------------------------------- #
# ContextVar 读写
# ---------------------------------------------------------------------- #


def get_current_tenant() -> TenantContext | None:
    return _tenant_ctx.get()


def set_tenant_context(ctx: TenantContext | None) -> contextvars.Token:
    return _tenant_ctx.set(ctx)


def reset_tenant_context(token: contextvars.Token) -> None:
    _tenant_ctx.reset(token)


@asynccontextmanager
async def tenant_scope(ctx: TenantContext) -> AsyncIterator[None]:
    """在 with 块内激活租户上下文，退出时恢复。"""
    token = set_tenant_context(ctx)
    try:
        yield
    finally:
        reset_tenant_context(token)


# ---------------------------------------------------------------------- #
# Service（业务逻辑）
# ---------------------------------------------------------------------- #


async def register_tenant(name: str, email: str, password: str) -> dict:
    """注册新租户 + 第一个管理员用户。

    Returns:
        ``{tenant_id, api_key, user_id, email}`` —— api_key 仅此一次返回明文。
    """
    factory = get_session_factory()
    # 先查重
    async with factory() as session:
        existing = await get_user_by_email(session, email)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="email already registered",
            )

    api_key_plain = generate_api_key()
    api_key_hash = _hash_api_key(api_key_plain)
    tenant_id = uuid.uuid4().hex
    user_id = uuid.uuid4().hex

    async with factory() as session:
        async with session.begin():
            tenant = Tenant(
                id=tenant_id,
                name=name,
                api_key_hash=api_key_hash,
                plan="free",
            )
            session.add(tenant)
            await session.flush()  # 拿到 tenant.id 给 FK
            user = User(
                id=user_id,
                tenant_id=tenant.id,
                email=email,
                password_hash=hash_password(password),
                display_name=name,
                is_active=True,
            )
            session.add(user)

    return {
        "tenant_id": tenant_id,
        "api_key": api_key_plain,
        "user_id": user_id,
        "email": email,
    }


async def authenticate(email: str, password: str) -> tuple[TenantContext, str]:
    """登录：返回 (TenantContext, JWT)。失败抛 401。"""
    factory = get_session_factory()
    async with factory() as session:
        user = await get_user_by_email(session, email)
        if user is None or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid credentials",
            )
        if not verify_password(password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid credentials",
            )
        # 取 tenant（已 eager 由 user.tenant_id 关联，但此处单独查确保存在）
        from sqlalchemy import select

        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == user.tenant_id))
        ).scalar_one_or_none()
        if tenant is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="tenant missing",
            )
        ctx = TenantContext(
            tenant_id=tenant.id,
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
        )
    token = create_access_token(
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, email=ctx.email
    )
    return ctx, token


async def resolve_api_key(api_key: str) -> TenantContext | None:
    """用 API Key 直接解析租户（不绑定特定用户）。

    供 OpenSandbox 服务端按租户路由：调用方传 ``af-xxx``，
    找到 tenant 后用 tenant 内首个 active 用户作为默认 user_id。
    """
    if not api_key.startswith("af-"):
        return None
    api_key_hash = _hash_api_key(api_key)
    factory = get_session_factory()
    async with factory() as session:
        tenant = await get_tenant_by_api_key_hash(session, api_key_hash)
        if tenant is None:
            return None
        # 取该租户第一个 active 用户作为默认归属（避免长期记忆 namespace 落空）
        from sqlalchemy import select

        user = (
            await session.execute(
                select(User)
                .where(User.tenant_id == tenant.id, User.is_active.is_(True))
                .order_by(User.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if user is None:
            return None
        return TenantContext(
            tenant_id=tenant.id,
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
        )


# ---------------------------------------------------------------------- #
# FastAPI 依赖 / 中间件
# ---------------------------------------------------------------------- #


async def _extract_credential(request: Request) -> HTTPAuthorizationCredentials | None:
    """从请求头取 Bearer token；兼容手动解析（避免 starlette 双重认证）。"""
    auth = request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


async def get_current_user_dep(request: Request) -> TenantContext:
    """FastAPI 依赖：解析 JWT → TenantContext。

    解析失败抛 401。成功时把上下文写入 ContextVar（同请求后续业务可取）。
    """
    cred = await _extract_credential(request)
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(cred.credentials)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    ctx = TenantContext(
        tenant_id=payload["tenant_id"],
        user_id=payload["sub"],
        email=payload["email"],
    )
    # 同步写入 ContextVar（FastAPI 请求在 task 中执行，ContextVar 隔离安全）
    set_tenant_context(ctx)
    return ctx


def get_optional_tenant() -> TenantContext | None:
    """业务层便捷读取当前租户（无则 None）。"""
    return get_current_tenant()


async def require_tenant() -> TenantContext:
    """业务层便捷读取当前租户（无则抛 403）。"""
    ctx = get_current_tenant()
    if ctx is None:
        # 兜底：依赖未触发但业务层调用，尝试从 ContextVar 再取一次
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant context missing",
        )
    return ctx
