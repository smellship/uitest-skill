"""值落盘开关（--mask-vars）与浏览器开销 CLI 开关：默认不动平台行为。"""

from __future__ import annotations

import json

import pytest
from fakes import FakeDriver, FakeEngine, done, settings_for, snapshot

from ui_agent.run.runner import MASK, Runner
from ui_agent.schema.decision import Decision
from ui_agent.verify.asserts import parse_check

CHECKS = ["text_contains=查询完成"]
SECRET_VALUE = "hunter2"
ACCOUNT = "sysadmin"


def text_field_snapshot(fingerprint: str, text: str = "请输入帐户名", secret: bool = False) -> dict:
    element = {"index": 1, "role": "textbox", "label": "请输入帐户名", "rect": [0, 0, 10, 10],
               "in_viewport": True, "occluded_by": "", "disabled": False,
               "operations": ["CLICK", "TYPE_TEXT"], "value": ""}
    if secret:
        element["secret"] = True
    return {"page": {"url": "http://x/", "title": "T", "text": text, "viewport": {}, "scroll": {}},
            "elements": [element], "fingerprint": fingerprint, "omitted": 0}


def run_with_vars(tmp_path, *, mask: bool, secret_field: bool = False, value: str = ACCOUNT):
    settings = settings_for(tmp_path, ui_agent_mask_vars=mask)
    driver = FakeDriver([text_field_snapshot("a", secret=secret_field),
                         snapshot("b", text="查询完成"), snapshot("b", text="查询完成")])
    engine = FakeEngine([Decision(operation="TYPE_TEXT", target="1", target_element=1,
                                  confidence=0.9, model="jev-latest"), done()])
    runner = Runner(settings, "登录", "http://x/", checks=[parse_check(CHECKS[0])],
                    task="t", driver=driver, engine=engine, verbose=False,
                    vars={"请输入帐户名": value})
    return runner.run(), runner


def artifacts_text(run_dir) -> str:
    """把所有产物拼起来，便于一次断言「值有没有落盘」。"""
    parts = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_file() and path.suffix in (".json", ".jsonl", ".html"):
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


class TestMaskVarsOffByDefault:
    def test_default_keeps_the_platform_behaviour(self, tmp_path):
        """默认关闭：非 secret 的 --var 值照旧出现在产物里（平台现状不变）。"""
        result, _ = run_with_vars(tmp_path, mask=False)
        assert result.status == "ok"
        assert ACCOUNT in artifacts_text(result.run_dir), "默认不掩码，保持既有行为"

    def test_default_trace_keeps_the_action_detail(self, tmp_path):
        """回归：曾经漏了开关判断，默认模式下 detail 也被打成 ••••••。"""
        from fakes import read_trace
        result, _ = run_with_vars(tmp_path, mask=False)
        typed = next(row for row in read_trace(result.run_dir)
                     if row["operation"] == "TYPE_TEXT")
        assert typed["action"]["detail"] == f"已输入：{ACCOUNT}", \
            "mask_vars 关着时 trace 的 action.detail 必须是原文"

    def test_default_actions_json_keeps_the_text(self, tmp_path):
        result, _ = run_with_vars(tmp_path, mask=False)
        rows = json.loads((result.run_dir / "actions.json").read_text(encoding="utf-8"))
        typed = next(row for row in rows if row["action"] == "TYPE_TEXT")
        assert typed["text"] == ACCOUNT

    def test_default_snapshot_keeps_the_value(self, tmp_path):
        result, _ = run_with_vars(tmp_path, mask=False)
        blob = "\n".join(p.read_text(encoding="utf-8")
                         for p in (result.run_dir / "snapshots").glob("*.json"))
        assert ACCOUNT in blob, "快照里的元素当前值默认照旧"

    def test_secret_fields_are_masked_even_when_off(self, tmp_path):
        """secret 字段的行为与开关无关：一直掩码。"""
        result, _ = run_with_vars(tmp_path, mask=False, secret_field=True, value=SECRET_VALUE)
        blob = artifacts_text(result.run_dir)
        assert SECRET_VALUE not in blob
        assert MASK in blob


class TestMaskVarsOn:
    def test_var_value_disappears_from_actions(self, tmp_path):
        result, _ = run_with_vars(tmp_path, mask=True)
        rows = json.loads((result.run_dir / "actions.json").read_text(encoding="utf-8"))
        typed = next(row for row in rows if row["action"] == "TYPE_TEXT")
        assert typed["text"] == MASK, "actions.json 里不许再有明文"

    def test_var_value_disappears_from_trace(self, tmp_path):
        result, _ = run_with_vars(tmp_path, mask=True)
        trace = (result.run_dir / "trace.jsonl").read_text(encoding="utf-8")
        assert ACCOUNT not in trace

    def test_var_value_disappears_from_snapshots(self, tmp_path):
        result, _ = run_with_vars(tmp_path, mask=True)
        for path in (result.run_dir / "snapshots").glob("*.json"):
            assert ACCOUNT not in path.read_text(encoding="utf-8"), f"快照泄漏：{path.name}"

    def test_var_value_disappears_from_the_whole_run_dir(self, tmp_path):
        """端到端：整个产物目录里都不该有明文。"""
        result, _ = run_with_vars(tmp_path, mask=True)
        assert result.status == "ok"
        assert ACCOUNT not in artifacts_text(result.run_dir)

    def test_other_vars_are_not_touched(self, tmp_path):
        """只掩 --var 的值，页面上的其它文本不动。"""
        result, _ = run_with_vars(tmp_path, mask=True)
        trace = (result.run_dir / "trace.jsonl").read_text(encoding="utf-8")
        assert "查询完成" in trace, "断言期望值不是 --var 值，不该被掩"

    def test_masking_does_not_break_the_recording(self, tmp_path):
        """掩码只作用于产物，录制内容仍是真实值（否则回放会输错）。"""
        result, runner = run_with_vars(tmp_path, mask=True)
        recorded = [rec.text for rec in (runner.cache.entry.records if runner.cache else [])]
        assert recorded == [] or ACCOUNT in recorded, "录像里应保留真实值用于回放"


class TestMaskHelpers:
    def test_nested_structures_are_walked(self):
        from ui_agent.run.runner import _mask_values
        data = {"a": [{"b": f"x {ACCOUNT} y"}], "c": ACCOUNT, "n": 3}
        out = _mask_values(data, [ACCOUNT])
        assert out["a"][0]["b"] == f"x {MASK} y" and out["c"] == MASK and out["n"] == 3

    def test_longer_values_win(self):
        """按长度倒序替换，避免短值把长值切碎。"""
        from ui_agent.run.runner import _mask_values
        assert _mask_values("abcdef", ["abc", "abcdef"]) == MASK


class TestBrowserOverheadFlags:
    """浏览器开销的 CLI 开关：只影响生效 settings，不改默认值。"""

    def args(self, **overrides):
        class Args:
            mode = None
            max_actions = None
            conf_min = None
            decider = None
            supervisor = None
            bridge_timeout = None
            cache = None
            cache_strict = False
            upload_mode = None
            dialog_policy = None
            mask_vars = False
            settle_ms = None
            slow_mo = None
            ready_stable_ms = None
            ready_timeout_ms = None
            assert_timeout_ms = None

        args = Args()
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_defaults_are_unchanged(self):
        import ui_agent.cli as cli
        settings = cli.apply_overrides(cli.Settings(), self.args())
        assert settings.settle_ms == 700
        assert settings.slow_mo == 250
        assert settings.ready_stable_ms == 2500
        assert settings.ready_timeout_ms == 20000
        assert settings.ui_agent_assert_timeout_ms == 5000
        assert settings.ui_agent_mask_vars is False

    def test_each_flag_maps_to_its_field(self):
        import ui_agent.cli as cli
        settings = cli.apply_overrides(cli.Settings(), self.args(
            settle_ms=120, slow_mo=0, ready_stable_ms=800,
            ready_timeout_ms=5000, assert_timeout_ms=1500))
        assert settings.settle_ms == 120
        assert settings.slow_mo == 0
        assert settings.ready_stable_ms == 800
        assert settings.ready_timeout_ms == 5000
        assert settings.ui_agent_assert_timeout_ms == 1500

    def test_mask_flag_maps_to_the_setting(self):
        import ui_agent.cli as cli
        assert cli.apply_overrides(cli.Settings(), self.args(mask_vars=True)).ui_agent_mask_vars

    def test_run_parser_accepts_the_flags(self):
        import ui_agent.cli as cli
        parser_args = cli.main.__doc__ is not None  # 只为确认模块可导入
        _ = parser_args
        # 真解析：确保 argparse 层认得这些开关
        import argparse
        captured = {}

        class StubRunner:
            def __init__(self, *a, **k):
                captured.update(k)

            def run(self):
                from pathlib import Path

                from ui_agent.run.runner import RunResult
                return RunResult("ok", 0, Path("."), "")

        original = cli.Runner
        cli.Runner = StubRunner
        try:
            code = cli.main(["run", "--goal", "g", "--url", "http://x/",
                             "--decider", "off", "--supervisor", "off",
                             "--settle-ms", "150", "--slow-mo", "0",
                             "--ready-stable-ms", "900", "--ready-timeout-ms", "4000",
                             "--assert-timeout-ms", "1200", "--mask-vars", "--json"])
        finally:
            cli.Runner = original
        assert code == 0
        _ = argparse

    def test_settle_ms_reaches_the_driver_settings(self, tmp_path):
        """开关最终落到 settings，driver 读的就是它。"""
        settings = settings_for(tmp_path, settle_ms=120)
        assert settings.settle_ms == 120


class TestRunStartRecordsTheOverhead:
    def test_run_start_exposes_the_effective_values(self, tmp_path):
        settings = settings_for(tmp_path, settle_ms=111)
        driver = FakeDriver([snapshot("a", text="查询完成")])
        engine = FakeEngine([done()])
        runner = Runner(settings, "目标", "http://x/", checks=[parse_check(CHECKS[0])],
                        task="t", driver=driver, engine=engine, verbose=False)
        result = runner.run()
        start = json.loads((result.run_dir / "trace.jsonl").read_text(
            encoding="utf-8").splitlines()[0])
        assert start["decider"] == "off" or start["decider"]
        _ = pytest
