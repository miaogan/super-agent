"""V1 Skill 加载器单元测试（用临时目录，不依赖外部服务）。

覆盖：
- frontmatter 解析（正常 / 无 frontmatter / 错误 YAML）
- 目录扫描：global + tenant 隔离
- 路径逃逸防护（``..`` / 符号链接）
- register_skill / delete_skill 写文件
- get_skill_dirs 给 deepagents 的目录列表
- 非法 tenant_id / skill_name 拒绝
"""

from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

import pytest

import app.config
import app.skill_loader
from app.skill_loader import (
    SkillMeta,
    _parse_frontmatter,
    delete_skill,
    get_skill_dirs,
    list_skills,
    register_skill,
)


VALID_TENANT = "a" * 32  # UUID hex


@pytest.fixture
def skills_dir(monkeypatch):
    """每个测试一个临时 skills_dir，重新 reload 模块让 settings 生效。"""
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("SKILLS_DIR", td)
        # reload 让 settings 重新读 env
        importlib.reload(app.config)
        importlib.reload(app.skill_loader)
        # 重新导入符号到当前命名空间
        yield td
    # 还原
    monkeypatch.delenv("SKILLS_DIR", raising=False)
    importlib.reload(app.config)
    importlib.reload(app.skill_loader)


# ---------------------------------------------------------------------- #
# frontmatter 解析
# ---------------------------------------------------------------------- #


def test_parse_frontmatter_normal():
    text = "---\nname: hello\ndescription: \"a skill\"\n---\n# hello\nbody"
    meta, body = _parse_frontmatter(text)
    assert meta["name"] == "hello"
    assert meta["description"] == "a skill"
    assert body == "# hello\nbody"


def test_parse_frontmatter_no_frontmatter():
    text = "# plain markdown\nno frontmatter"
    meta, body = _parse_frontmatter(text)
    assert meta == {}
    assert body == text.strip()


def test_parse_frontmatter_unclosed_acts_as_no_meta():
    text = "---\nname: hello\nbody without closer"
    meta, body = _parse_frontmatter(text)
    assert meta == {}
    assert body == text.strip()


def test_parse_frontmatter_quoted_values_stripped():
    text = '---\nname: "quoted"\ndescription: \'single\'\n---\nbody'
    meta, _ = _parse_frontmatter(text)
    assert meta["name"] == "quoted"
    assert meta["description"] == "single"


def test_parse_frontmatter_ignores_comments_and_blank():
    text = "---\n# a comment\n\nname: x\n---\nbody"
    meta, body = _parse_frontmatter(text)
    assert meta == {"name": "x"}
    assert body == "body"


# ---------------------------------------------------------------------- #
# 目录扫描 + 隔离
# ---------------------------------------------------------------------- #


def _write_skill(base: Path, *segs: str, content: str = "# hi") -> None:
    p = base.joinpath(*segs)
    p.mkdir(parents=True, exist_ok=True)
    (p / "SKILL.md").write_text(content, encoding="utf-8")


def test_list_skills_global_only(skills_dir):
    base = Path(skills_dir)
    _write_skill(base, "global", "greet", content="---\nname: greet\n---\n# greet")
    items = list_skills(VALID_TENANT)
    assert len(items) == 1
    assert items[0].name == "greet"
    assert items[0].is_global is True
    assert items[0].tenant_id is None


def test_list_skills_tenant_isolation(skills_dir):
    base = Path(skills_dir)
    other_tenant = "b" * 32
    _write_skill(base, "global", "shared", content="---\nname: shared\n---\nshared")
    _write_skill(base, "tenants", VALID_TENANT, "mine", content="---\nname: mine\n---\nmine")
    _write_skill(base, "tenants", other_tenant, "theirs", content="---\nname: theirs\n---\ntheirs")

    mine = list_skills(VALID_TENANT)
    names = {it.name for it in mine}
    assert names == {"shared", "mine"}
    assert "theirs" not in names  # 看不到其他租户的


def test_list_skills_skips_invalid_names(skills_dir):
    base = Path(skills_dir)
    # 非法字符的目录名应被跳过
    _write_skill(base, "global", "valid", content="---\nname: valid\n---\nx")
    base.joinpath("global", "bad..name").mkdir(parents=True)
    (base / "global" / "bad..name" / "SKILL.md").write_text("x")
    items = list_skills(VALID_TENANT)
    names = {it.name for it in items}
    assert "valid" in names
    assert "bad..name" not in names


def test_list_skills_empty_when_no_dir(skills_dir):
    items = list_skills(VALID_TENANT)
    assert items == []


# ---------------------------------------------------------------------- #
# register_skill / delete_skill
# ---------------------------------------------------------------------- #


def test_register_skill_writes_file_with_frontmatter(skills_dir):
    p = register_skill(VALID_TENANT, "my-skill", content="# body", description="desc")
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: my-skill" in text
    assert 'description: "desc"' in text
    assert text.endswith("# body")


def test_register_skill_then_list(skills_dir):
    register_skill(VALID_TENANT, "my-skill", content="# body")
    items = list_skills(VALID_TENANT)
    assert any(it.name == "my-skill" and not it.is_global for it in items)


def test_register_skill_rejects_invalid_tenant(skills_dir):
    with pytest.raises(ValueError):
        register_skill("not-a-uuid", "x", content="x")


def test_register_skill_rejects_invalid_name(skills_dir):
    with pytest.raises(ValueError):
        register_skill(VALID_TENANT, "bad..name", content="x")
    with pytest.raises(ValueError):
        register_skill(VALID_TENANT, "with space", content="x")


def test_delete_skill_removes_dir(skills_dir):
    register_skill(VALID_TENANT, "tmp", content="x")
    assert delete_skill(VALID_TENANT, "tmp") is True
    items = list_skills(VALID_TENANT)
    assert all(it.name != "tmp" for it in items)


def test_delete_skill_missing_returns_false(skills_dir):
    assert delete_skill(VALID_TENANT, "nope") is False


def test_delete_skill_invalid_args_returns_false(skills_dir):
    assert delete_skill("not-uuid", "x") is False
    assert delete_skill(VALID_TENANT, "bad..name") is False


# ---------------------------------------------------------------------- #
# get_skill_dirs（给 deepagents skills= 参数）
# ---------------------------------------------------------------------- #


def test_get_skill_dirs_includes_existing(skills_dir):
    base = Path(skills_dir)
    base.joinpath("global").mkdir(parents=True)
    base.joinpath("tenants", VALID_TENANT).mkdir(parents=True)
    dirs = get_skill_dirs(VALID_TENANT)
    assert len(dirs) == 2
    assert any(p.endswith("global") for p in dirs)
    assert any(VALID_TENANT in p for p in dirs)


def test_get_skill_dirs_skips_missing(skills_dir):
    # 只有 global，没有 tenant 目录
    base = Path(skills_dir)
    base.joinpath("global").mkdir(parents=True)
    dirs = get_skill_dirs(VALID_TENANT)
    assert len(dirs) == 1
    assert dirs[0].endswith("global")


def test_get_skill_dirs_invalid_tenant_returns_only_global(skills_dir):
    base = Path(skills_dir)
    base.joinpath("global").mkdir(parents=True)
    dirs = get_skill_dirs("not-a-uuid")
    assert len(dirs) == 1


# ---------------------------------------------------------------------- #
# 路径逃逸防护
# ---------------------------------------------------------------------- #


def test_register_skill_rejects_path_escape_via_name(skills_dir):
    # 名字含 .. 应被正则拒绝
    with pytest.raises(ValueError):
        register_skill(VALID_TENANT, "..", content="x")


def test_list_skills_ignores_symlink_escape(skills_dir, tmp_path):
    if os.name == "nt":
        pytest.skip("符号链接在 Windows 上需管理员权限")
    base = Path(skills_dir)
    target = tmp_path / "secret"
    target.mkdir()
    (target / "SKILL.md").write_text("---\nname: leak\n---\nleaked", encoding="utf-8")
    gd = base / "global"
    gd.mkdir(parents=True)
    # 在 global 下放一个指向 secret 的符号链接目录
    try:
        os.symlink(target, gd / "leak", target_is_directory=True)
    except OSError:
        pytest.skip("无法创建符号链接")
    items = list_skills(VALID_TENANT)
    # resolve() 后会跑到 skills_dir 之外，被 _safe_resolve 拒绝
    assert all(it.name != "leak" for it in items), "符号链接逃逸应被拒绝"
