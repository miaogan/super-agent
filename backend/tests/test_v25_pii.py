"""V3-T8：PII 检测与脱敏测试。

覆盖：
- 各类型检测（邮箱 / 手机号 / 身份证 / IP / 银行卡）+ 重叠合并
- 三种脱敏模式（mask / partial / redact）
- 结构化字段脱敏（mask_pii_fields）
- 配置解析（pii_config_from_env + monkeypatch）
- mask_value 单值脱敏
"""

from __future__ import annotations

import pytest

from app.pii import (
    MASK_MASK,
    MASK_MODES,
    MASK_PARTIAL,
    MASK_REDACT,
    PII_CREDIT_CARD,
    PII_EMAIL,
    PII_ID_CARD_CN,
    PII_IP,
    PII_PHONE_CN,
    PII_TYPES,
    PIIConfig,
    detect_pii,
    mask_pii,
    mask_pii_fields,
    mask_value,
    pii_config_from_env,
)


# ---------------------------------------------------------------------- #
# 检测
# ---------------------------------------------------------------------- #


def test_detect_email():
    text = "联系 a.b+tag@Example.com 或 admin@x.org"
    matches = detect_pii(text)
    types = {m.type for m in matches}
    assert PII_EMAIL in types
    email = next(m for m in matches if m.type == PII_EMAIL)
    assert email.value.lower() == "a.b+tag@example.com"
    assert text[email.start : email.end] == "a.b+tag@Example.com"


def test_detect_phone_cn():
    text = "手机 13812345678，不是 2381234567"
    matches = detect_pii(text, types=[PII_PHONE_CN])
    assert [m.value for m in matches] == ["13812345678"]


def test_detect_id_card_cn():
    text = "证件号 110101199003074512"
    matches = detect_pii(text, types=[PII_ID_CARD_CN])
    assert matches and matches[0].value == "110101199003074512"


def test_detect_ip():
    text = "来源 10.20.30.40，网关 255.255.255.255"
    matches = detect_pii(text, types=[PII_IP])
    assert {m.value for m in matches} == {"10.20.30.40", "255.255.255.255"}


def test_detect_credit_card():
    text = "卡号 6222 0213 4567 8901"
    matches = detect_pii(text, types=[PII_CREDIT_CARD])
    assert matches and "6222" in matches[0].value


def test_detect_types_filter():
    text = "a@x.com 13812345678"
    emails = detect_pii(text, types=[PII_EMAIL])
    assert all(m.type == PII_EMAIL for m in emails)
    assert len(emails) == 1
    phones = detect_pii(text, types=[PII_PHONE_CN])
    assert len(phones) == 1
    assert phones[0].value == "13812345678"


def test_detect_empty_and_overlap():
    assert detect_pii("") == []
    # 邮箱局部与 IP 形式重叠时按 start 升序、长命中优先
    text = "hello@world.com"
    matches = detect_pii(text)
    starts = [m.start for m in matches]
    assert starts == sorted(starts)


# ---------------------------------------------------------------------- #
# 脱敏
# ---------------------------------------------------------------------- #


def test_mask_partial():
    text = "邮箱 a@x.com 手机 13812345678 身份证 110101199003074512"
    masked, matches = mask_pii(text, mode=MASK_PARTIAL)
    assert "a***@x.com" in masked  # 邮箱保留域名 + 首字符
    assert "138****5678" in masked
    assert "110101" in masked and masked.count("********") == 1
    assert len(matches) == 3


def test_mask_full():
    text = "联系 a@x.com"
    masked, _ = mask_pii(text, mode=MASK_MASK)
    assert "******" in masked
    assert "@" not in masked


def test_mask_redact():
    text = "手机 13812345678"
    masked, _ = mask_pii(text, mode=MASK_REDACT)
    assert "[REDACTED]" in masked
    assert "13812345678" not in masked


def test_mask_value_single():
    assert mask_value(PII_PHONE_CN, "13812345678") == "138****5678"
    assert mask_value(PII_EMAIL, "a@x.com") == "a***@x.com"
    assert mask_value(PII_ID_CARD_CN, "110101199003074512") == "110101********4512"
    assert mask_value(PII_IP, "10.20.30.40") == "10.20.***.***"
    assert mask_value(PII_CREDIT_CARD, "6222021345678901").startswith("6222")
    with pytest.raises(ValueError):
        mask_value(PII_EMAIL, "a@x.com", mode="bogus")


def test_mask_no_hits_returns_original():
    text = "没有任何敏感信息"
    masked, matches = mask_pii(text)
    assert masked == text
    assert matches == []


def test_mask_bad_mode():
    with pytest.raises(ValueError):
        mask_pii("x", mode="bogus")


# ---------------------------------------------------------------------- #
# 结构化字段脱敏
# ---------------------------------------------------------------------- #


def test_mask_pii_fields_dict():
    payload = {
        "user": {"email": "a@x.com", "name": "Ann"},
        "phone": "13812345678",
        "note": "无敏感",
    }
    out = mask_pii_fields(payload, mode=MASK_PARTIAL)
    assert out["user"]["email"] == "a***@x.com"
    assert out["phone"] == "138****5678"
    assert out["note"] == "无敏感"


def test_mask_pii_fields_secrets():
    payload = {
        "password": "s3cr3t-abc",
        "client_secret": "long-secret-value",
        "token": "tok123",
    }
    out = mask_pii_fields(payload, mode=MASK_PARTIAL)
    assert out["password"] != "s3cr3t-abc"
    assert "*" in out["password"]
    assert out["client_secret"] != "long-secret-value"
    assert out["token"] == "***" or out["token"].endswith("**")


def test_mask_pii_fields_list_and_nested():
    payload = {"items": [{"email": "a@x.com"}, {"id_card": "110101199003074512"}]}
    out = mask_pii_fields(payload, mode=MASK_REDACT)
    assert out["items"][0]["email"] == "[REDACTED]"
    assert out["items"][1]["id_card"] == "[REDACTED]"


def test_mask_pii_fields_bad_mode():
    with pytest.raises(ValueError):
        mask_pii_fields({"a": 1}, mode="bogus")


# ---------------------------------------------------------------------- #
# 配置
# ---------------------------------------------------------------------- #


def test_pii_config_default():
    cfg = pii_config_from_env({})
    assert cfg.mode == MASK_PARTIAL
    assert cfg.types is None


def test_pii_config_from_env(monkeypatch):
    monkeypatch.setenv("PII_MASK_MODE", "redact")
    monkeypatch.setenv("PII_MASK_TYPES", "email, phone_cn, bogus")
    cfg = pii_config_from_env()
    assert cfg.mode == MASK_REDACT
    assert cfg.types == frozenset({PII_EMAIL, PII_PHONE_CN})
    assert cfg.to_mapping()["types"] == sorted([PII_EMAIL, PII_PHONE_CN])


def test_pii_config_bad_mode_fallback():
    cfg = pii_config_from_env({"PII_MASK_MODE": "weird"})
    assert cfg.mode == MASK_PARTIAL


def test_constants():
    assert MASK_MODES == {MASK_MASK, MASK_PARTIAL, MASK_REDACT}
    assert PII_TYPES == {
        PII_EMAIL,
        PII_PHONE_CN,
        PII_ID_CARD_CN,
        PII_IP,
        PII_CREDIT_CARD,
    }
    cfg = PIIConfig(mode="mask", types=frozenset({PII_EMAIL}))
    assert cfg.to_mapping()["mode"] == "mask"
