"""技能层测试（设计方案 §4.5：热加载 + 自学 + 自进化沉淀）。"""

from __future__ import annotations

import textwrap

import pytest

from hermes_superagent.skills import (
    SKILL_FILENAME,
    SkillRegistry,
    discover,
    load_skill,
    split_front_matter,
)


def _write_skill(root, name: str, front: str, body: str = "步骤\n") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / SKILL_FILENAME).write_text(f"---\n{front}\n---\n\n{body}", encoding="utf-8")


# ----------------------------------------------------------------------
# front matter 解析
# ----------------------------------------------------------------------
def test_split_front_matter_parses_metadata_and_body():
    meta, body = split_front_matter(
        "---\nname: demo\ndescription: 演示\n---\n\n正文在此\n"
    )
    assert meta["name"] == "demo"
    assert meta["description"] == "演示"
    assert body.strip() == "正文在此"


def test_split_front_matter_without_metadata_returns_whole_text():
    meta, body = split_front_matter("# 没有 front matter\n")
    assert meta == {}
    assert "没有 front matter" in body


def test_split_front_matter_tolerates_bom():
    meta, _ = split_front_matter("\ufeff---\nname: bom\n---\n\nx\n")
    assert meta["name"] == "bom"


def test_split_front_matter_raises_on_broken_yaml():
    from hermes_superagent.skills import SkillError

    with pytest.raises(SkillError):
        split_front_matter("---\nname: [unclosed\n---\n\nx\n")


# ----------------------------------------------------------------------
# 加载与校验
# ----------------------------------------------------------------------
def test_load_skill_reads_fields(tmp_path):
    _write_skill(tmp_path, "demo", "name: demo\ndescription: 演示\ntriggers: [a, b]\ntools: crawl\nversion: 2")
    s = load_skill(tmp_path / "demo")
    assert s.name == "demo"
    assert s.description == "演示"
    assert s.triggers == ("a", "b")
    assert s.tools == ("crawl",)
    assert s.version == "2"
    assert s.warnings == []


def test_load_skill_falls_back_and_warns_when_required_missing(tmp_path):
    """缺必填字段不阻断，但必须留可见告警（不静默）。"""
    _write_skill(tmp_path, "nameless", "triggers: [x]")
    s = load_skill(tmp_path / "nameless")
    assert s.name == "nameless"          # 回退目录名
    assert s.description == ""
    assert len(s.warnings) >= 2          # name 与 description 各一条


def test_discover_returns_errors_not_raises(tmp_path):
    """单个技能坏掉不该拖垮整层，但错误必须被上抛记录。"""
    _write_skill(tmp_path, "good", "name: good\ndescription: 好的")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / SKILL_FILENAME).write_text("---\nname: [broken\n---\n", encoding="utf-8")

    skills, errors = discover(tmp_path)
    assert [s.name for s in skills] == ["good"]
    assert len(errors) == 1
    assert "bad" in errors[0]["dir"]


def test_discover_ignores_dirs_without_skill_file(tmp_path):
    (tmp_path / "not-a-skill").mkdir()
    skills, errors = discover(tmp_path)
    assert skills == [] and errors == []


def test_discover_missing_root_is_not_an_error(tmp_path):
    skills, errors = discover(tmp_path / "nope")
    assert skills == [] and errors == []


# ----------------------------------------------------------------------
# 注册表：热加载
# ----------------------------------------------------------------------
def test_registry_loads_at_create(tmp_path):
    _write_skill(tmp_path, "alpha", "name: alpha\ndescription: A")
    reg = SkillRegistry.create(tmp_path)
    assert [s.name for s in reg.list()] == ["alpha"]


def test_registry_hot_reload_picks_up_new_skill(tmp_path):
    """丢一个目录进去即生效——这正是 §4.5 的扩展方式。"""
    _write_skill(tmp_path, "alpha", "name: alpha\ndescription: A")
    reg = SkillRegistry.create(tmp_path)
    assert len(reg.skills) == 1

    _write_skill(tmp_path, "beta", "name: beta\ndescription: B")
    report = reg.reload()

    assert report["changed"] is True
    assert report["added"] == ["beta"]
    assert sorted(reg.skills) == ["alpha", "beta"]


def test_registry_reload_detects_removal(tmp_path):
    _write_skill(tmp_path, "alpha", "name: alpha\ndescription: A")
    _write_skill(tmp_path, "beta", "name: beta\ndescription: B")
    reg = SkillRegistry.create(tmp_path)

    import shutil

    shutil.rmtree(tmp_path / "beta")
    report = reg.reload()

    assert report["removed"] == ["beta"]
    assert sorted(reg.skills) == ["alpha"]


def test_registry_reload_reports_no_change(tmp_path):
    _write_skill(tmp_path, "alpha", "name: alpha\ndescription: A")
    reg = SkillRegistry.create(tmp_path)
    report = reg.reload()
    assert report["changed"] is False
    assert report["added"] == [] and report["updated"] == []


# ----------------------------------------------------------------------
# 检索
# ----------------------------------------------------------------------
def test_match_by_trigger(tmp_path):
    _write_skill(tmp_path, "alpha", "name: alpha\ndescription: A\ntriggers: [抓取, crawl]")
    reg = SkillRegistry.create(tmp_path)
    hits = reg.match("帮我抓取这个页面")
    assert [s.name for s in hits] == ["alpha"]


def test_match_by_name_and_description(tmp_path):
    _write_skill(tmp_path, "web-digest", "name: web-digest\ndescription: 网页摘要")
    reg = SkillRegistry.create(tmp_path)
    assert [s.name for s in reg.match("digest")] == ["web-digest"]
    assert [s.name for s in reg.match("摘要")] == ["web-digest"]


def test_match_empty_query_returns_nothing(tmp_path):
    _write_skill(tmp_path, "alpha", "name: alpha\ndescription: A\ntriggers: [x]")
    reg = SkillRegistry.create(tmp_path)
    assert reg.match("") == []
    assert reg.match("   ") == []


def test_match_ranks_exact_trigger_higher(tmp_path):
    _write_skill(tmp_path, "loose", "name: loose\ndescription: 描述里提到 crawl")
    _write_skill(tmp_path, "exact", "name: exact\ndescription: X\ntriggers: [crawl]")
    reg = SkillRegistry.create(tmp_path)
    assert reg.match("crawl")[0].name == "exact"


def test_match_respects_limit(tmp_path):
    for i in range(4):
        _write_skill(tmp_path, f"s{i}", f"name: s{i}\ndescription: 摘要 {i}")
    reg = SkillRegistry.create(tmp_path)
    assert len(reg.match("摘要", limit=2)) == 2


# ----------------------------------------------------------------------
# 自进化：scaffold
# ----------------------------------------------------------------------
def test_scaffold_creates_and_hotloads(tmp_path):
    reg = SkillRegistry.create(tmp_path)
    res = reg.scaffold("my-fix", "把一次有效解法固化", triggers=("修复",), tools=("fs",))
    assert res["ok"] is True
    assert (tmp_path / "my-fix" / SKILL_FILENAME).exists()
    # 立即生效，无需重启
    assert "my-fix" in reg.skills
    assert reg.get("my-fix").triggers == ("修复",)


def test_scaffold_refuses_to_overwrite(tmp_path):
    reg = SkillRegistry.create(tmp_path)
    reg.scaffold("dup", "第一次")
    res = reg.scaffold("dup", "第二次")
    assert res["ok"] is False
    assert "已存在" in res["error"]
    assert res["blocked_at"]           # 拒绝必须指明卡点
    assert "第一次" in (tmp_path / "dup" / SKILL_FILENAME).read_text(encoding="utf-8")


def test_scaffold_sanitizes_name(tmp_path):
    reg = SkillRegistry.create(tmp_path)
    res = reg.scaffold("my skill/../evil", "净化测试")
    assert res["ok"] is True
    assert res["name"] == "my-skill-evil"
    assert (tmp_path / "my-skill-evil").is_dir()


def test_scaffold_rejects_empty_name(tmp_path):
    reg = SkillRegistry.create(tmp_path)
    res = reg.scaffold("///", "空名")
    assert res["ok"] is False
    assert res["blocked_at"]


# ----------------------------------------------------------------------
# Capability 面（单一出口）
# ----------------------------------------------------------------------
def test_skill_capability_actions(tmp_path):
    import asyncio

    from hermes_superagent.capabilities.skill import SkillCapability

    _write_skill(tmp_path, "alpha", "name: alpha\ndescription: A\ntriggers: [抓取]")
    cap = SkillCapability(SkillRegistry.create(tmp_path))

    async def run():
        listed = await cap.invoke("list", {})
        assert listed["ok"] and listed["data"]["count"] == 1

        got = await cap.invoke("get", {"name": "alpha"})
        assert got["ok"] and "body" in got["data"]

        missed = await cap.invoke("get", {"name": "nope"})
        assert missed["ok"] is False and "不存在" in missed["error"]

        matched = await cap.invoke("match", {"query": "抓取"})
        assert matched["ok"] and len(matched["data"]["hits"]) == 1

        reloaded = await cap.invoke("reload", {})
        assert reloaded["ok"]

        made = await cap.invoke("scaffold", {"name": "beta", "description": "B"})
        assert made["ok"]

        dup = await cap.invoke("scaffold", {"name": "beta", "description": "B2"})
        assert dup["ok"] is False and dup["blocked_at"]

    asyncio.run(run())


def test_skill_capability_is_registered_in_default_runtime():
    """技能层必须挂进默认 runtime，否则宿主/MCP 看不到它。"""
    from hermes_superagent.agent_harness import build_default_runtime

    rt = build_default_runtime(token="t")
    assert "skill" in rt.capabilities


def test_repo_ships_a_sample_skill():
    """仓库自带示例技能，保证 clone 下来就有可用的扩展样例。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "skills"
    skills, errors = discover(root)
    assert errors == []
    assert any(s.name == "web-digest" for s in skills)
    sample = next(s for s in skills if s.name == "web-digest")
    assert sample.triggers, "示例技能应有触发词"
    assert "抓取" in sample.body or "crawl" in sample.body
