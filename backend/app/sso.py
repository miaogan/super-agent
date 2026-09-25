"""V3-T8：SSO（OAuth2 授权码最小子集）+ 账号绑定。

范围（不追求 SAML / PKCE 全覆盖）
-------------------------------
- **授权码流**：``authorize``（构造授权 URL）→ ``exchange_code``（兑换 token）
  → ``fetch_userinfo``（拉用户信息）→ ``sso_login``（绑定/创建用户 + 签发 JWT）
- **Stub IdP**：进程内身份提供商，不依赖外部服务器即可走完整授权码流
  （测试 + 演示用；真实 IdP 用 httpx 传输）
- **绑定表**：``SSOAccount``（provider + subject 唯一 → 租户内用户）
- **state 信封**：base64url(JSON) 携带 ``tenant_id`` / ``redirect_uri`` / ``exp``，
  回调时校验防伪造与过期

传输可注入：``exchange_code`` / ``fetch_userinfo`` 接受可选的 ``transport``
（带 ``post(url, form)`` / ``get(url, headers)`` 的对象），默认走 httpx；
测试传 ``StubIdP`` 即可离线跑通全流程。
"""

from __future__ import annotations

import base64
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

# ---------------------------------------------------------------------- #
# 常量
# ---------------------------------------------------------------------- #

DEFAULT_SCOPES = "openid profile email"
STATE_TTL_SECONDS = 600
STUB_PROVIDER = "stub"

# 常见 IdP 的 userinfo 字段映射：raw -> canonical
# canonical: {"subject", "email", "display_name"}
_PROVIDER_USERINFO_MAP: dict[str, dict[str, str]] = {
    "google": {"subject": "sub", "email": "email", "display_name": "name"},
    "github": {"subject": "id", "email": "email", "display_name": "login"},
    "stub": {"subject": "sub", "email": "email", "display_name": "name"},
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------- #
# 异常
# ---------------------------------------------------------------------- #


class SSOError(Exception):
    """SSO 基类异常。"""


class SSOConfigError(SSOError):
    """provider 配置缺失 / 非法。"""


class SSOExchangeError(SSOError):
    """授权码兑换失败。"""


class SSOUserInfoError(SSOError):
    """用户信息拉取失败。"""


class SSOStateError(SSOError):
    """state 校验失败（过期 / 篡改）。"""


# ---------------------------------------------------------------------- #
# 配置
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class OAuth2ProviderConfig:
    """OAuth2 授权码流 provider 配置。"""

    name: str
    client_id: str
    client_secret: str
    authorization_url: str
    token_url: str
    userinfo_url: str
    scopes: str = DEFAULT_SCOPES
    # 固定 redirect_uri；None 时由调用方按请求动态给
    redirect_uri: str | None = None


def _env_sso_provider(name: str, prefix: str) -> OAuth2ProviderConfig | None:
    """从环境变量读取一个 provider；缺任一关键项返回 None。"""
    import os

    client_id = os.getenv(f"{prefix}_CLIENT_ID")
    client_secret = os.getenv(f"{prefix}_CLIENT_SECRET")
    authorization_url = os.getenv(f"{prefix}_AUTHORIZATION_URL")
    token_url = os.getenv(f"{prefix}_TOKEN_URL")
    userinfo_url = os.getenv(f"{prefix}_USERINFO_URL")
    if not all([client_id, client_secret, authorization_url, token_url, userinfo_url]):
        return None
    return OAuth2ProviderConfig(
        name=name,
        client_id=client_id,
        client_secret=client_secret,
        authorization_url=authorization_url,
        token_url=token_url,
        userinfo_url=userinfo_url,
        scopes=os.getenv(f"{prefix}_SCOPES", DEFAULT_SCOPES),
    )


def sso_providers_from_env() -> dict[str, OAuth2ProviderConfig]:
    """从环境变量发现 SSO providers。

    支持任意 provider：``SSO_<NAME>_CLIENT_ID`` / ``_CLIENT_SECRET`` /
    ``_AUTHORIZATION_URL`` / ``_TOKEN_URL`` / ``_USERINFO_URL`` / ``_SCOPES``
    （``<NAME>`` 为大写，如 ``SSO_GOOGLE_*``）。

    默认内置 ``stub`` provider（离线演示，可用 ``SSO_STUB_ENABLED=0`` 关闭）。
    """
    import os

    providers: dict[str, OAuth2ProviderConfig] = {}
    # 通用前缀发现：SSO_<NAME>_CLIENT_ID
    for key, val in os.environ.items():
        if not (key.startswith("SSO_") and key.endswith("_CLIENT_ID") and val):
            continue
        name = key[len("SSO_"):-len("_CLIENT_ID")].lower()
        if not name or name == "stub":
            continue
        prefix = f"SSO_{name.upper()}"
        cfg = _env_sso_provider(name, prefix)
        if cfg is not None:
            providers[name] = cfg
    # stub 演示 provider
    if os.getenv("SSO_STUB_ENABLED", "1").lower() not in ("0", "false", "no", "off"):
        providers.setdefault(STUB_PROVIDER, stub_provider_config())
    return providers


def stub_provider_config() -> OAuth2ProviderConfig:
    """内置 stub provider 配置（端点指向本服务的 stub IdP 路由）。"""
    return OAuth2ProviderConfig(
        name=STUB_PROVIDER,
        client_id="stub-client",
        client_secret="stub-secret",
        # 这两个 URL 仅用于生成授权链接；stub 传输会自行处理
        authorization_url="https://idp.stub.local/authorize",
        token_url="https://idp.stub.local/token",
        userinfo_url="https://idp.stub.local/userinfo",
        scopes="openid profile email",
    )


def get_sso_providers() -> dict[str, OAuth2ProviderConfig]:
    """当前可用的 SSO providers（配置 + stub）。"""
    return sso_providers_from_env()


def require_sso_provider(name: str) -> OAuth2ProviderConfig:
    providers = sso_providers_from_env()
    cfg = providers.get(name)
    if cfg is None:
        raise SSOConfigError(f"SSO provider 未配置: {name}")
    return cfg


# ---------------------------------------------------------------------- #
# state 信封
# ---------------------------------------------------------------------- #


def encode_state(*, tenant_id: str, redirect_uri: str, provider: str) -> str:
    """构造 state：base64url(JSON)，含过期时间。"""
    payload = {
        "tenant_id": tenant_id,
        "redirect_uri": redirect_uri,
        "provider": provider,
        "exp": int(time.time()) + STATE_TTL_SECONDS,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_state(state: str, *, provider: str) -> dict[str, Any]:
    """解析并校验 state；失败抛 ``SSOStateError``。"""
    try:
        raw = base64.urlsafe_b64decode(state.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise SSOStateError("state 非法") from exc
    if payload.get("provider") != provider:
        raise SSOStateError("state provider 不匹配")
    exp = payload.get("exp") or 0
    if time.time() > int(exp):
        raise SSOStateError("state 已过期")
    if not payload.get("tenant_id"):
        raise SSOStateError("state 缺少 tenant_id")
    return payload


# ---------------------------------------------------------------------- #
# 传输层
# ---------------------------------------------------------------------- #


class _HttpxTransport:
    """默认传输：真实 httpx 调用外部 IdP。"""

    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def post(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(url, data=form)
        if resp.status_code != 200:
            raise SSOExchangeError(
                f"token 端点返回 HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise SSOExchangeError(f"token 端点响应非法: {resp.text[:200]}") from exc

    async def get(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise SSOUserInfoError(
                f"userinfo 端点返回 HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise SSOUserInfoError(f"userinfo 端点响应非法: {resp.text[:200]}") from exc


class StubIdP:
    """进程内 stub 身份提供商（测试 / 离线演示）。

    用法：``idp = StubIdP()``；``idp.register_user("a@x.com", "A")``；
    授权码流：``code = idp.authorize("a@x.com")`` →
    ``idp.exchange_code("a@x.com")`` → ``idp.userinfo(token)``。
    也可作为 ``transport`` 传给 ``sso_login``（走同样的 post/get 接口）。
    """

    def __init__(self) -> None:
        self.users: dict[str, dict[str, str]] = {}  # email -> {"email", "name", "sub"}
        self._codes: dict[str, str] = {}  # code -> email
        self._tokens: dict[str, str] = {}  # token -> email

    # ---- 用户管理 ----

    def register_user(self, email: str, name: str | None = None) -> None:
        self.users[email] = {
            "email": email,
            "name": name or email.split("@")[0],
            "sub": f"sub_{uuid.uuid4().hex[:12]}",
        }

    def list_users(self) -> list[dict[str, str]]:
        return [dict(u) for u in self.users.values()]

    # ---- 授权码流 ----

    def authorize(self, email: str) -> str:
        """模拟 IdP 同意页：给已注册用户签发一次性授权码。"""
        if email not in self.users:
            raise SSOError(f"stub IdP 用户不存在: {email}")
        code = secrets.token_urlsafe(24)
        self._codes[code] = email
        return code

    async def exchange_code(self, code: str) -> str:
        """兑换授权码 → access_token。"""
        email = self._codes.pop(code, None)
        if email is None:
            raise SSOExchangeError("stub 授权码无效或已使用")
        token = "stub_" + secrets.token_urlsafe(24)
        self._tokens[token] = email
        return token

    async def userinfo(self, token: str) -> dict[str, str]:
        """token → 用户信息。"""
        email = self._tokens.get(token)
        if email is None:
            raise SSOUserInfoError("stub token 无效")
        return dict(self.users[email])

    # ---- transport 接口（与 _HttpxTransport 对齐，供 sso_login 复用）----

    async def post(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        if url.endswith("/token"):
            code = form.get("code") or ""
            token = await self.exchange_code(code)
            return {"access_token": token, "token_type": "bearer"}
        raise SSOError(f"stub IdP 不认识的端点: {url}")

    async def get(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        if url.endswith("/userinfo"):
            auth = headers.get("Authorization") or ""
            if not auth.lower().startswith("bearer "):
                raise SSOUserInfoError("stub userinfo 缺 Bearer token")
            return await self.userinfo(auth[7:].strip())
        raise SSOError(f"stub IdP 不认识的端点: {url}")


# ---------------------------------------------------------------------- #
# OAuth2 流程函数
# ---------------------------------------------------------------------- #


def build_authorization_url(
    config: OAuth2ProviderConfig,
    *,
    state: str,
    redirect_uri: str | None = None,
) -> str:
    """构造授权 URL（authorization code flow 第一步）。"""
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "scope": config.scopes,
        "state": state,
        "redirect_uri": redirect_uri or config.redirect_uri or "",
    }
    sep = "&" if "?" in config.authorization_url else "?"
    return f"{config.authorization_url}{sep}{urlencode(params)}"


async def exchange_code(
    config: OAuth2ProviderConfig,
    code: str,
    redirect_uri: str | None = None,
    transport: Any | None = None,
) -> dict[str, Any]:
    """兑换授权码 → token 响应（需含 ``access_token``）。"""
    t = transport or _HttpxTransport()
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "redirect_uri": redirect_uri or config.redirect_uri or "",
    }
    resp = await t.post(config.token_url, form)
    if not isinstance(resp, dict) or not resp.get("access_token"):
        raise SSOExchangeError(f"token 响应缺少 access_token: {resp!r}")
    return resp


async def fetch_userinfo(
    config: OAuth2ProviderConfig,
    access_token: str,
    transport: Any | None = None,
) -> dict[str, Any]:
    """拉取用户信息。"""
    t = transport or _HttpxTransport()
    resp = await t.get(
        config.userinfo_url,
        {"Authorization": f"Bearer {access_token}"},
    )
    if not isinstance(resp, dict):
        raise SSOUserInfoError(f"userinfo 响应非法: {resp!r}")
    return resp


def normalize_userinfo(provider: str, raw: dict[str, Any]) -> dict[str, str]:
    """把 provider 的 userinfo 映射为规范字段。

    Returns:
        ``{"subject", "email", "display_name"}``
    """
    mapping = _PROVIDER_USERINFO_MAP.get(provider, _PROVIDER_USERINFO_MAP["google"])
    subject = str(raw.get(mapping["subject"]) or "").strip()
    email = str(raw.get(mapping["email"]) or "").strip().lower()
    display_name = raw.get(mapping["display_name"]) or None
    if not subject:
        subject = email
    if not subject:
        raise SSOUserInfoError("userinfo 缺少 subject/email")
    return {
        "subject": subject,
        "email": email or subject,
        "display_name": str(display_name) if display_name else None,
    }


# ---------------------------------------------------------------------- #
# 登录服务
# ---------------------------------------------------------------------- #


async def sso_login(
    config: OAuth2ProviderConfig,
    code: str,
    redirect_uri: str | None = None,
    *,
    tenant_id: str,
    session_factory: Any | None = None,
    transport: Any | None = None,
) -> dict[str, Any]:
    """完整 SSO 登录：兑换 → userinfo → 绑定/创建用户 → 签发 JWT。

    Args:
        config: provider 配置
        code: 授权码
        redirect_uri: 回调地址
        tenant_id: 目标租户（由 state 解析）
        session_factory: 可注入（测试传 SQLite sessionmaker）
        transport: 可注入（测试传 StubIdP）

    Returns:
        ``{"access_token", "token_type", "tenant_id", "user_id", "email",
          "display_name", "bound"}`` —— bound 表示本次是否新建绑定
    """
    from sqlalchemy import select

    from app.auth import create_access_token
    from app.models import SSOAccount, Tenant, User, get_session_factory

    factory = session_factory or get_session_factory()
    token_resp = await exchange_code(config, code, redirect_uri, transport=transport)
    access_token = token_resp["access_token"]
    raw = await fetch_userinfo(config, access_token, transport=transport)
    info = normalize_userinfo(config.name, raw)

    async with factory() as session:
        async with session.begin():
            tenant = (
                await session.execute(
                    select(Tenant).where(Tenant.id == tenant_id)
                )
            ).scalar_one_or_none()
            if tenant is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="tenant not found",
                )

            account = (
                await session.execute(
                    select(SSOAccount).where(
                        SSOAccount.provider == config.name,
                        SSOAccount.subject == info["subject"],
                    )
                )
            ).scalar_one_or_none()

            bound = False
            if account is not None:
                # 已绑定：更新快照，取既有用户
                account.email = info["email"]
                account.display_name = info["display_name"]
                user = (
                    await session.execute(
                        select(User).where(User.id == account.user_id)
                    )
                ).scalar_one_or_none()
            else:
                # 未绑定：租户内按 email 找用户，没有则创建（SSO 专属）
                user = (
                    await session.execute(
                        select(User).where(
                            User.tenant_id == tenant_id,
                            User.email == info["email"],
                        )
                    )
                ).scalar_one_or_none()
                if user is None:
                    user = User(
                        id=_new_id("u"),
                        tenant_id=tenant_id,
                        email=info["email"],
                        password_hash="!",  # SSO 专属用户不可密码登录
                        display_name=info["display_name"],
                        is_active=True,
                    )
                    session.add(user)
                    await session.flush()
                account = SSOAccount(
                    id=_new_id("sso"),
                    tenant_id=tenant_id,
                    user_id=user.id,
                    provider=config.name,
                    subject=info["subject"],
                    email=info["email"],
                    display_name=info["display_name"],
                )
                session.add(account)
                bound = True

            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="sso user missing",
                )

    jwt_token = create_access_token(
        tenant_id=tenant_id,
        user_id=user.id,
        email=user.email,
    )
    return {
        "access_token": jwt_token,
        "token_type": "bearer",
        "tenant_id": tenant_id,
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "bound": bound,
    }
