"""分层沙箱测试（设计方案 §9B / §9B.4）。

覆盖：
- 四层默认策略（§9B.1）
- 文件/Shell：工作区可写、系统只读区不可写、沙箱外拒绝（且必带 blocked_at）
- 硬约束 1：搜索永不被拦
- 硬约束 2/3：安装通道开放；装不上必定显式指明卡点
- 外接 CDP：默认禁 + 显式 opt-in + 留痕
- 拇指浏览器：可交互、住沙箱、独立 profile
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hermes_superagent.sandbox import (
    SandboxLayer,
    build_sandbox,
    system_readonly_roots,
)


@pytest.fixture()
def sb(tmp_path: Path):
    return build_sandbox(tmp_path)


# ----------------------------------------------------------------------
# 四层策略
# ----------------------------------------------------------------------
def test_four_layers_present(sb):
    for layer in SandboxLayer:
        assert layer in sb.policy.layers


def test_kernel_layer_is_not_sandboxed(sb):
    """§9B.1：内核层无沙箱（本就隔离进程，无额外风险）。"""
    assert sb.policy.is_sandboxed(SandboxLayer.KERNEL) is False


def test_file_shell_browser_cdp_are_sandboxed(sb):
    assert sb.policy.is_sandboxed(SandboxLayer.FILE_SHELL) is True
    assert sb.policy.is_sandboxed(SandboxLayer.BROWSER) is True
    assert sb.policy.is_sandboxed(SandboxLayer.EXTERNAL_CDP) is True


def test_external_cdp_requires_opt_in(sb):
    """§9B.1：外接 CDP 默认禁。"""
    assert sb.policy.layer(SandboxLayer.EXTERNAL_CDP).requires_opt_in is True


# ----------------------------------------------------------------------
# 文件 / Shell 守卫
# ----------------------------------------------------------------------
def test_workspace_is_writable(sb, tmp_path: Path):
    d = sb.fs.check_write(tmp_path / "notes.md")
    assert d.allow is True


def test_workspace_subdir_is_writable(sb, tmp_path: Path):
    d = sb.fs.check_write(tmp_path / "a" / "b" / "c.txt")
    assert d.allow is True


def test_outside_sandbox_write_is_denied_with_blocked_at(sb):
    """沙箱外写入拒绝，且必须指明卡点（§9B.4-3）。"""
    outside = Path(tmp_path_root()) / "definitely-not-in-workspace-xyz.txt"
    d = sb.fs.check_write(outside)
    assert d.allow is False
    assert d.blocked_at, "拒绝必须带 blocked_at（禁止静默）"


def test_system_readonly_root_read_allowed_write_denied(sb):
    """§9B.1：系统关键路径可读不可写。"""
    roots = system_readonly_roots()
    if not roots:
        pytest.skip("当前平台无可用系统只读区样本")
    target = roots[0] / "hermes-superagent-should-not-be-created.txt"

    read = sb.fs.check_read(target)
    assert read.allow is True

    write = sb.fs.check_write(target)
    assert write.allow is False
    assert write.blocked_at and "只读区" in write.blocked_at


def test_shell_command_writing_readonly_root_is_denied(sb):
    roots = system_readonly_roots()
    if not roots:
        pytest.skip("当前平台无可用系统只读区样本")
    cmd = f'echo hacked > "{roots[0]}\\evil.txt"'
    d = sb.fs.check_shell(cmd)
    assert d.allow is False
    assert d.blocked_at


def test_shell_read_command_is_allowed(sb):
    d = sb.fs.check_shell("python --version")
    assert d.allow is True


def test_shell_is_not_whitelisted_to_death(sb):
    """§9B.4-2：不做命令白名单——否则「装不上东西」的锅就甩给沙箱了。"""
    for cmd in (
        "pip install httpx",
        "npm install tsdown",
        "python -m pytest tests -q",
        "git status",
    ):
        assert sb.fs.check_shell(cmd).allow is True, cmd


# ----------------------------------------------------------------------
# 硬约束 1：搜索永不被拦
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "query",
    ["普通搜索", "site:github.com agent", "验证码 怎么过", "敏感词测试", "🔥 unicode"],
)
def test_search_is_never_intercepted(sb, query):
    d = sb.policy.guard_search(query)
    assert d.allow is True
    assert d.blocked_at is None


def test_captcha_goes_to_human_not_auto_bypass(sb):
    """§9B.4-1：人机验证交人工，不自动绕、不硬闯。"""
    cap = sb.policy.captcha_policy()
    assert cap["action"] == "hand_to_human"
    assert cap["auto_bypass"] is False
    assert cap["surface"] == "thumb_browser"


# ----------------------------------------------------------------------
# 硬约束 2/3：安装开放 + 装不上必定指明卡点
# ----------------------------------------------------------------------
def test_install_targets_are_allowed(sb, tmp_path: Path):
    """§9B.4-2：包安装路径放行（宽进严管）。"""
    d = sb.fs.check_install_path(tmp_path / "site-packages" / "pkg")
    assert d.allow is True


def test_install_report_never_silent(sb):
    rep = sb.install.report()
    assert "checks" in rep and len(rep["checks"]) >= 2
    # 每个不可用项都必须带 blocked_at
    for c in rep["checks"]:
        if not c["available"]:
            assert c["blocked_at"], f"{c['tool']} 不可用却没说卡在哪"


@pytest.mark.parametrize(
    "output,expect_kw",
    [
        ("ERROR: Could not find a version that satisfies the requirement foo", "找不到该包"),
        ("Connection timed out after 30s", "网络出口不可达"),
        ("ERROR: Permission denied: '/usr/lib/python3'", "文件权限被拒"),
        ("No space left on device", "磁盘空间不足"),
        ("Temporary failure in name resolution", "DNS 解析失败"),
        ("SSLError: certificate verify failed", "TLS 证书校验失败"),
    ],
)
def test_install_failure_is_attributed(sb, output, expect_kw):
    """§9B.4-3：装不上必须显式告警并**指明卡在哪**。"""
    d = sb.install.diagnose_failure("pip", output=output, exit_code=1)
    assert d.allow is False
    assert d.blocked_at, "必须给出卡点"
    assert expect_kw in d.blocked_at or expect_kw in d.reason


def test_unknown_install_failure_still_names_a_point(sb):
    """未知失败也不许静默——必须给出手上有什么、下一步做什么。"""
    d = sb.install.diagnose_failure("npm", output="weird unknown error", exit_code=2)
    assert d.allow is False
    assert d.blocked_at
    assert str(2) in d.blocked_at or "未归因" in d.blocked_at


def test_empty_failure_output_is_not_silent(sb):
    d = sb.install.diagnose_failure("pip", output="", exit_code=None)
    assert d.allow is False
    assert d.blocked_at and "禁止静默" in d.blocked_at


def test_install_diagnostics_summary_renders(sb):
    text = sb.install.summary()
    assert "依赖安装环境体检" in text
    assert "宽进严管" in text


# ----------------------------------------------------------------------
# 外接 CDP：默认禁 + 显式 opt-in + 留痕
# ----------------------------------------------------------------------
def test_cdp_is_denied_by_default(sb):
    d = sb.browser.check_cdp()
    assert d.allow is False
    assert d.blocked_at and "未授权" in d.blocked_at
    assert sb.browser.cdp_authorized is False


def test_cdp_requires_supervisor(sb):
    d = sb.browser.enable_cdp("http://127.0.0.1:9222", supervisor="", reason="需要登录态")
    assert d.allow is False
    assert "supervisor" in (d.blocked_at or "")


def test_cdp_requires_reason(sb):
    d = sb.browser.enable_cdp("http://127.0.0.1:9222", supervisor="alice", reason="  ")
    assert d.allow is False
    assert "reason" in (d.blocked_at or "")


def test_cdp_requires_endpoint(sb):
    d = sb.browser.enable_cdp("", supervisor="alice", reason="需要登录态")
    assert d.allow is False
    assert "endpoint" in (d.blocked_at or "")


def test_cdp_opt_in_succeeds_and_leaves_evidence(sb):
    d = sb.browser.enable_cdp(
        "http://127.0.0.1:9222", supervisor="alice", reason="需要复用已登录会话"
    )
    assert d.allow is True
    assert sb.browser.cdp_authorized is True
    assert sb.browser.check_cdp().allow is True

    log = sb.browser.evidence_log()
    assert any(e["event"] == "cdp.enabled" for e in log)
    entry = next(e for e in log if e["event"] == "cdp.enabled")
    assert entry["supervisor"] == "alice"
    assert entry["reason"] == "需要复用已登录会话"
    assert entry["endpoint"] == "http://127.0.0.1:9222"


def test_cdp_can_be_disabled(sb):
    sb.browser.enable_cdp("http://127.0.0.1:9222", supervisor="alice", reason="x")
    sb.browser.disable_cdp(operator="alice")
    assert sb.browser.cdp_authorized is False
    assert sb.browser.check_cdp().allow is False


# ----------------------------------------------------------------------
# 浏览器沙箱：隔离 profile + 拇指浏览器
# ----------------------------------------------------------------------
def test_browser_uses_isolated_profile_not_daily_browser(sb, tmp_path: Path):
    """§9B.2：默认不碰用户日常浏览器。"""
    cfg = sb.browser.builtin_config()
    assert cfg.mode == "builtin"
    assert str(tmp_path) in str(cfg.profile_dir)
    assert ".sandbox" in str(cfg.profile_dir)


def test_launch_args_use_isolated_profile(sb):
    cfg = sb.browser.builtin_config(headless=True)
    args = sb.browser.launch_args(cfg)
    assert any(a.startswith("--user-data-dir=") for a in args)
    assert any("--headless" in a for a in args)
    # 绝不指向用户日常 profile
    joined = " ".join(args).lower()
    assert "default" not in joined or "user-data-dir" in joined


def test_thumb_browser_is_interactive_and_in_sandbox(sb):
    """§9A.3 / §13.2：拇指浏览器可交互，且住沙箱。"""
    rep = sb.browser.report()
    thumb = rep["thumb_browser"]
    assert thumb["interactive"] is True
    assert thumb["runs_in_sandbox"] is True
    assert thumb["default_size"] == {"width": 360, "height": 640, "fps": 4.0}


def test_browser_report_marks_cdp_default_disabled(sb):
    rep = sb.browser.report()
    assert rep["cdp_authorized"] is False


# ----------------------------------------------------------------------
# 汇总输出
# ----------------------------------------------------------------------
def test_policy_to_dict_declares_hard_constraints(sb):
    d = sb.policy.to_dict()
    hc = d["hard_constraints"]
    assert hc["search_never_intercepted"] is True
    assert hc["install_open_wide_in_strict_out"] is True
    assert hc["block_must_name_the_point"] is True


def test_policy_summary_renders_all_layers(sb):
    text = sb.policy.summary()
    for layer in SandboxLayer:
        assert layer.value in text
    assert "硬约束" in text


def test_sandbox_to_dict_is_serializable(sb):
    import json

    json.dumps(sb.to_dict(), ensure_ascii=False)


def tmp_path_root() -> str:
    """跨平台取一个肯定在 workspace 之外的根。"""
    if sys.platform.startswith("win"):
        return "C:\\"
    return "/"
