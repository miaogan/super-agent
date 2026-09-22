"""V1 Skill 加载器：文件系统 + frontmatter 解析。

目录约定（``SKILLS_DIR`` 默认 ``skills``）::

    skills/
    ├─ global/                       # 所有租户可见
    │  └─ <skill_name>/
    │     └─ SKILL.md
    └─ tenants/
       └─ <tenant_id>/              # 该租户专属
          └─ <skill_name>/
             └─ SKILL.md

SKILL.md 格式（YAML frontmatter + Markdown 主体）::

    ---
    name: write-pyfile
    description: 在沙箱写文件并执行
    ---
    # write-pyfile
    使用步骤：...
    （自由 Markdown 内容，作为 deepagents ``skills`` 参数传入）

安全约束
--------
- 文件路径仅允许 ``[A-Za-z0-9_-]+`` 的 skill name，禁止 ``..`` 跨目录。
- tenant_id 仅允许 hex 字符（UUID）。
- 全部走 ``Path.resolve()`` 后校验是否仍在 ``skills_dir`` 之内，防符号链接逃逸。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TENANT_ID_RE = re.compile(r"^[a-fA-F0-9]{32}$")  # UUID hex
_SKILL_FILENAME = "SKILL.md"


@dataclass(frozen=True)
class SkillMeta:
    """Skill 元数据 + 内容快照。"""

    name: str
    description: str
    content: str
    is_global: bool
    tenant_id: str | None
    path: str  # 文件系统绝对路径


def _safe_resolve(path: Path, base: Path) -> Path | None:
    """解析 path 后校验是否在 base 内（防符号链接/``..`` 逃逸）。"""
    try:
        resolved = path.resolve()
        base_resolved = base.resolve()
        resolved.relative_to(base_resolved)
        return resolved
    except (ValueError, OSError):
        return None


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """极简 YAML frontmatter 解析（不依赖 PyYAML）。

    仅支持 ``key: value`` 行 + ``---`` 分隔。复杂 YAML 不需要——
    skill metadata 只有 name/description 两个标量。
    """
    meta: dict[str, str] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return meta, text.strip()
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return meta, text.strip()
    for line in lines[1:end_idx]:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            continue
        key, _, value = s.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    body = "\n".join(lines[end_idx + 1 :]).strip()
    return meta, body


def _read_skill_file(file_path: Path) -> tuple[dict[str, str], str] | None:
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("读取 skill 失败 %s: %s", file_path, exc)
        return None
    return _parse_frontmatter(text)


def _scan_dir(dir_path: Path, is_global: bool, tenant_id: str | None) -> list[SkillMeta]:
    """扫描一个目录下的全部 skill 子目录。

    对每个子目录做 ``_safe_resolve`` 校验，防止符号链接逃逸出 ``skills_dir``。
    """
    out: list[SkillMeta] = []
    if not dir_path.is_dir():
        return out
    base = _skills_base()
    for child in dir_path.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not _SKILL_NAME_RE.match(name):
            continue
        # 防符号链接逃逸：resolve 后必须仍在 skills_dir 内
        if _safe_resolve(child, base) is None:
            logger.warning("跳过逃逸 skills_dir 的路径: %s", child)
            continue
        skill_file = child / _SKILL_FILENAME
        if _safe_resolve(skill_file, base) is None or not skill_file.is_file():
            continue
        parsed = _read_skill_file(skill_file)
        if parsed is None:
            continue
        meta, body = parsed
        out.append(
            SkillMeta(
                name=meta.get("name", name),
                description=meta.get("description", ""),
                content=body,
                is_global=is_global,
                tenant_id=tenant_id,
                path=str(skill_file),
            )
        )
    return out


# ---------------------------------------------------------------------- #
# 对外 API
# ---------------------------------------------------------------------- #


def _skills_base() -> Path:
    return Path(settings.skills_dir).resolve()


def _global_dir() -> Path:
    return _skills_base() / "global"


def _tenant_dir(tenant_id: str) -> Path:
    return _skills_base() / "tenants" / tenant_id


def list_skills(tenant_id: str | None = None) -> list[SkillMeta]:
    """列出 global + 该租户专属的全部 skill。"""
    base = _skills_base()
    out: list[SkillMeta] = []
    out.extend(_scan_dir(_global_dir(), is_global=True, tenant_id=None))
    if tenant_id is not None:
        if not _TENANT_ID_RE.match(tenant_id):
            logger.warning("非法 tenant_id: %s", tenant_id)
            return out
        out.extend(
            _scan_dir(_tenant_dir(tenant_id), is_global=False, tenant_id=tenant_id)
        )
    return out


def get_skill_dirs(tenant_id: str | None = None) -> list[str]:
    """返回 deepagents ``skills`` 参数所需目录列表。

    deepagents ``skills`` 接受目录路径列表，会自动扫描其中的
    ``<name>/SKILL.md``。本函数返回 global + tenant 目录中存在的目录。
    """
    base = _skills_base()
    dirs: list[str] = []
    gd = _global_dir()
    if _safe_resolve(gd, base) is not None and gd.is_dir():
        dirs.append(str(gd))
    if tenant_id is not None and _TENANT_ID_RE.match(tenant_id):
        td = _tenant_dir(tenant_id)
        if _safe_resolve(td, base) is not None and td.is_dir():
            dirs.append(str(td))
    return dirs


def register_skill(
    tenant_id: str, name: str, content: str, description: str = ""
) -> Path:
    """写入租户专属 skill 文件。

    自动加 frontmatter（name/description）。返回文件路径。
    """
    if not _TENANT_ID_RE.match(tenant_id):
        raise ValueError(f"invalid tenant_id: {tenant_id}")
    if not _SKILL_NAME_RE.match(name):
        raise ValueError(f"invalid skill name: {name}")

    base = _skills_base()
    skill_dir = _tenant_dir(tenant_id) / name
    resolved = _safe_resolve(skill_dir, base)
    if resolved is None:
        raise ValueError("skill path escapes skills_dir")
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / _SKILL_FILENAME
    frontmatter = "---\n"
    frontmatter += f"name: {name}\n"
    frontmatter += f'description: "{description}"\n'
    frontmatter += "---\n\n"
    skill_file.write_text(frontmatter + content, encoding="utf-8")
    return skill_file


def delete_skill(tenant_id: str, name: str) -> bool:
    """删除租户专属 skill。global skill 不可通过此接口删除。"""
    if not _TENANT_ID_RE.match(tenant_id) or not _SKILL_NAME_RE.match(name):
        return False
    base = _skills_base()
    skill_dir = _tenant_dir(tenant_id) / name
    resolved = _safe_resolve(skill_dir, base)
    if resolved is None or not skill_dir.is_dir():
        return False
    import shutil

    shutil.rmtree(skill_dir)
    return True
