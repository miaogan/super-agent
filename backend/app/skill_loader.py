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


# ---------------------------------------------------------------------- #
# 压缩包上传（复杂 skill：含辅助脚本/资源文件/多文件）
# ---------------------------------------------------------------------- #

#: 压缩包内允许的文件扩展名白名单（防止上传可执行恶意文件）
_ARCHIVE_ALLOWED_EXT = {
    ".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
    ".html", ".css", ".sql", ".sh", ".csv", ".xml", ".toml", ".ini",
    ".cfg", ".conf", ".rst", ".png", ".jpg", ".jpeg", ".gif", ".svg",
}

#: 压缩包解压后最大文件数
_ARCHIVE_MAX_FILES = 200
#: 单文件最大 10MB
_ARCHIVE_MAX_FILE_SIZE = 10 * 1024 * 1024
#: 解压后总大小最大 50MB
_ARCHIVE_MAX_TOTAL_SIZE = 50 * 1024 * 1024


def register_skill_archive(
    tenant_id: str,
    name: str,
    archive_bytes: bytes,
    description: str = "",
) -> dict:
    """从 zip 压缩包注册复杂 skill（含多文件）。

    压缩包结构要求：
    - 必须包含 ``SKILL.md``（根目录或一级子目录）
    - 可包含辅助脚本、资源文件等（扩展名白名单限制）

    安全约束：
    - 文件名禁止 ``..`` / 绝对路径 / 符号链接
    - 扩展名白名单过滤
    - 文件数/大小限制

    Returns:
        dict: ``{"name", "description", "files", "skill_file"}``
    """
    import io
    import zipfile

    if not _TENANT_ID_RE.match(tenant_id):
        raise ValueError(f"invalid tenant_id: {tenant_id}")
    if not _SKILL_NAME_RE.match(name):
        raise ValueError(f"invalid skill name: {name}")

    # 解压到内存，先校验再落盘
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid zip file: {exc}") from exc

    entries: list[tuple[str, bytes]] = []
    total_size = 0
    has_skill_md = False

    for info in zf.infolist():
        if info.is_dir():
            continue
        fname = info.filename
        # 安全校验：禁止绝对路径 / .. / 符号链接
        if fname.startswith("/") or ".." in fname.split("/"):
            raise ValueError(f"unsafe path in archive: {fname}")
        # 扩展名白名单
        ext = Path(fname).suffix.lower()
        if ext not in _ARCHIVE_ALLOWED_EXT:
            raise ValueError(f"file type not allowed: {fname} (.{ext})")
        # 文件数限制
        if len(entries) >= _ARCHIVE_MAX_FILES:
            raise ValueError(
                f"too many files in archive (max {_ARCHIVE_MAX_FILES})"
            )
        data = zf.read(info)
        # 单文件大小限制
        if len(data) > _ARCHIVE_MAX_FILE_SIZE:
            raise ValueError(f"file too large: {fname} (max {_ARCHIVE_MAX_FILE_SIZE})")
        total_size += len(data)
        if total_size > _ARCHIVE_MAX_TOTAL_SIZE:
            raise ValueError(
                f"archive too large (max {_ARCHIVE_MAX_TOTAL_SIZE})"
            )
        # 检查是否有 SKILL.md
        basename = Path(fname).name
        if basename == _SKILL_FILENAME:
            has_skill_md = True
        entries.append((fname, data))

    zf.close()

    if not has_skill_md:
        raise ValueError(f"archive must contain {_SKILL_FILENAME}")

    # 落盘到 skill 目录
    base = _skills_base()
    skill_dir = _tenant_dir(tenant_id) / name
    resolved_base = _safe_resolve(skill_dir, base)
    if resolved_base is None:
        raise ValueError("skill path escapes skills_dir")

    import shutil

    # 清空旧目录（覆盖更新）
    if skill_dir.is_dir():
        shutil.rmtree(skill_dir)
    skill_dir.mkdir(parents=True, exist_ok=True)

    written_files: list[str] = []
    for fname, data in entries:
        # 构造安全的目标路径
        target = skill_dir / fname
        resolved = _safe_resolve(target, base)
        if resolved is None:
            raise ValueError(f"path escapes skills_dir: {fname}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written_files.append(fname)

    # 如果 SKILL.md 缺少 frontmatter，自动补上
    skill_file = skill_dir / _SKILL_FILENAME
    if skill_file.is_file():
        text = skill_file.read_text(encoding="utf-8")
        parsed = _parse_frontmatter(text)
        meta, body = parsed
        if "name" not in meta or "description" not in meta:
            frontmatter = "---\n"
            frontmatter += f"name: {name}\n"
            frontmatter += f'description: "{description}"\n'
            frontmatter += "---\n\n"
            skill_file.write_text(frontmatter + body, encoding="utf-8")

    logger.info(
        "压缩包 skill 注册成功: %s (%d files)", name, len(written_files)
    )
    return {
        "name": name,
        "description": description,
        "files": written_files,
        "skill_file": str(skill_file),
    }
