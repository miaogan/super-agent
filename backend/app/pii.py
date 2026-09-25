"""V3-T8：PII 检测与数据脱敏。

范围（最小子集）
---------------
- **检测**：基于正则识别常见 PII（邮箱 / 中国大陆手机号 / 身份证号 / IP / 银行卡）
- **脱敏**：三种模式
  - ``mask``：整段替换为 ``******``
  - ``partial``：保留部分明文字段（如 ``138****5678`` / ``a***@example.com``）
  - ``redact``：替换为 ``[REDACTED]``
- **结构化脱敏**：``mask_pii_fields`` 递归遍历 dict/list，对命名字段脱敏
- **配置**：``PII_MASK_MODE``（mask/partial/redact）+ ``PII_MASK_TYPES``（逗号分隔）

注：正则检测是启发式的，生产环境建议配合模型 / 词表；此处定位为
「默认开启的快速脱敏层」。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable

# ---------------------------------------------------------------------- #
# 常量
# ---------------------------------------------------------------------- #

PII_EMAIL = "email"
PII_PHONE_CN = "phone_cn"
PII_ID_CARD_CN = "id_card_cn"
PII_IP = "ip"
PII_CREDIT_CARD = "credit_card"
PII_TYPES = {
    PII_EMAIL,
    PII_PHONE_CN,
    PII_ID_CARD_CN,
    PII_IP,
    PII_CREDIT_CARD,
}

MASK_MASK = "mask"
MASK_PARTIAL = "partial"
MASK_REDACT = "redact"
MASK_MODES = {MASK_MASK, MASK_PARTIAL, MASK_REDACT}

_REDACTED = "[REDACTED]"

# 正则（按优先级检测，身份证在前避免与生日等误撞）
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        PII_ID_CARD_CN,
        re.compile(
            r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}"
            r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
        ),
    ),
    (
        PII_EMAIL,
        re.compile(r"(?<![\w.])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?!\w)"),
    ),
    (
        PII_PHONE_CN,
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ),
    (
        PII_CREDIT_CARD,
        re.compile(r"(?<!\d)(?:\d[ -]?){13,19}\d(?!\d)"),
    ),
    (
        PII_IP,
        re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
    ),
]

# 敏感字段名（mask_pii_fields 用）
_SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "api_key",
    "api_key_hash",
    "secret",
    "client_secret",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "email",
    "phone",
    "phone_number",
    "mobile",
    "id_card",
    "id_card_no",
    "credit_card",
    "card_number",
}


@dataclass(frozen=True)
class PIIMatch:
    """单条 PII 命中。"""

    type: str
    start: int
    end: int
    value: str

    def to_mapping(self, masked: str | None = None) -> dict[str, Any]:
        return {
            "type": self.type,
            "start": self.start,
            "end": self.end,
            "value": masked if masked is not None else self.value,
        }


@dataclass(frozen=True)
class PIIConfig:
    """脱敏配置。"""

    mode: str = MASK_PARTIAL
    types: frozenset[str] | None = None  # None = 全部

    def to_mapping(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "types": sorted(self.types) if self.types else None,
        }


def pii_config_from_env(env: dict[str, str] | None = None) -> PIIConfig:
    """从环境变量构建配置（测试可注入 env dict）。

    - ``PII_MASK_MODE``：mask / partial / redact（默认 partial）
    - ``PII_MASK_TYPES``：逗号分隔类型名（默认全部）
    """
    env = os.environ if env is None else env
    mode = env.get("PII_MASK_MODE", MASK_PARTIAL).strip().lower()
    if mode not in MASK_MODES:
        mode = MASK_PARTIAL
    raw_types = env.get("PII_MASK_TYPES", "").strip()
    types = None
    if raw_types:
        picked = {t.strip().lower() for t in raw_types.split(",") if t.strip()}
        picked &= PII_TYPES
        if picked:
            types = frozenset(picked)
    return PIIConfig(mode=mode, types=types)


# ---------------------------------------------------------------------- #
# 检测
# ---------------------------------------------------------------------- #


def detect_pii(
    text: str,
    types: Iterable[str] | None = None,
) -> list[PIIMatch]:
    """检测文本中的 PII，按 start 升序返回（重叠命中取更长者）。"""
    if not text:
        return []
    wanted = set(types) if types is not None else set(PII_TYPES)
    raw: list[PIIMatch] = []
    for ptype, pattern in _PATTERNS:
        if ptype not in wanted:
            continue
        for m in pattern.finditer(text):
            raw.append(PIIMatch(type=ptype, start=m.start(), end=m.end(), value=m.group()))
    # 按 (start, -len) 排序，优先保留长命中，吞掉被覆盖的短命中
    raw.sort(key=lambda m: (m.start, -(m.end - m.start)))
    merged: list[PIIMatch] = []
    last_end = -1
    for m in raw:
        if m.start >= last_end:
            merged.append(m)
            last_end = m.end
    return merged


# ---------------------------------------------------------------------- #
# 脱敏
# ---------------------------------------------------------------------- #


def _mask_value(ptype: str, value: str, mode: str) -> str:
    if mode == MASK_REDACT:
        return _REDACTED
    if mode == MASK_MASK:
        return "*" * min(len(value), 6)
    # partial：按类型保留首尾
    if ptype == PII_EMAIL:
        local, _, domain = value.partition("@")
        head = local[:1] + "*" * max(len(local) - 1, 3)
        return f"{head}@{domain}"
    if ptype == PII_PHONE_CN:
        return value[:3] + "****" + value[-4:]
    if ptype == PII_ID_CARD_CN:
        return value[:6] + "*" * (len(value) - 10) + value[-4:]
    if ptype == PII_CREDIT_CARD:
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 8:
            return value[:4] + " **** **** " + value[-4:]
        return "*" * len(value)
    if ptype == PII_IP:
        parts = value.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.***.***"
        return "*" * len(value)
    # 兜底：保留首 3 尾 3
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 6) + value[-3:]


def mask_value(ptype: str, value: str, mode: str = MASK_PARTIAL) -> str:
    """对单个 PII 值脱敏（供 API 返回脱敏后的命中值）。"""
    if mode not in MASK_MODES:
        raise ValueError(f"未知脱敏模式: {mode}")
    return _mask_value(ptype, value, mode)


def mask_pii(
    text: str,
    mode: str = MASK_PARTIAL,
    types: Iterable[str] | None = None,
) -> tuple[str, list[PIIMatch]]:
    """对文本脱敏。

    Returns:
        ``(masked_text, matches)``
    """
    if mode not in MASK_MODES:
        raise ValueError(f"未知脱敏模式: {mode}")
    matches = detect_pii(text, types)
    if not matches:
        return text, []
    chunks: list[str] = []
    pos = 0
    for m in matches:
        chunks.append(text[pos : m.start])
        chunks.append(_mask_value(m.type, m.value, mode))
        pos = m.end
    chunks.append(text[pos:])
    return "".join(chunks), matches


def _walk_mask(obj: Any, *, mode: str, types: frozenset[str] | None) -> Any:
    """递归遍历 dict/list，对敏感 key 的字符串值脱敏。"""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, str) and k.lower() in _SENSITIVE_KEYS:
                masked, _ = mask_pii(v, mode=mode, types=types)
                # 纯密钥类字段（无 PII 命中）整段打码，避免明文泄露
                out[k] = masked if masked != v else _mask_whole(v)
            else:
                out[k] = _walk_mask(v, mode=mode, types=types)
        return out
    if isinstance(obj, list):
        return [_walk_mask(v, mode=mode, types=types) for v in obj]
    return obj


def _mask_whole(value: str) -> str:
    if len(value) <= 6:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def mask_pii_fields(
    obj: Any,
    mode: str = MASK_PARTIAL,
    types: Iterable[str] | None = None,
) -> Any:
    """结构化脱敏：递归处理 dict/list 中的敏感字段。

    - 命中 PII 的字段按 ``mode`` 脱敏
    - 密钥类字段（password/token/secret...）即使无 PII 也整段打码
    """
    if mode not in MASK_MODES:
        raise ValueError(f"未知脱敏模式: {mode}")
    wanted = frozenset(types) if types is not None else None
    return _walk_mask(obj, mode=mode, types=wanted)


def mask_text_with_config(text: str, config: PIIConfig) -> dict[str, Any]:
    """按配置脱敏文本，返回响应友好结构。"""
    masked, matches = mask_pii(
        text,
        mode=config.mode,
        types=config.types,
    )
    return {
        "original": text,
        "masked": masked,
        "mode": config.mode,
        "matches": [m.to_mapping(masked=m) for m in matches],
    }
