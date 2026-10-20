"""探索模式：不录制、导出轨迹与用例骨架、骨架过不了编译（故意的）、轨迹不泄漏 --var 值。"""

from __future__ import annotations

import json

from fakes import FakeDriver, FakeEngine, done, read_rows, sealed_answers, settings_for, snapshot

from ui_agent.case.yaml_case import compile_case
from ui_agent.run.explore import (
    build_skeleton,
    build_trajectory,
    count_asks,
    explore,
    write_artifacts,
)
from ui_agent.run.runner import Runner
from ui_agent.schema.decision import Decision
from ui_agent.verify.asserts import parse_check

GOAL = "登录后查询票据并核对结果"


def text_field_snapshot(fingerprint: str, text: str = "请输入帐户名") -> dict:
    return {"page": {"url": "http://x/", "title": "T", "text": text, "viewport": {}, "scroll": {}},
            "elements": [{"index": 1, "role": "textbox", "label": "账号", "rect": [0, 0, 10, 10],
                          "in_viewport": True, "occluded_by": "", "disabled": False,
                          "operations": ["CLICK", "TYPE_TEXT"]}],
            "fingerprint": fingerprint, "omitted": 0}


class TestExploreDoesNotRecord:
    def test_explore_writes_no_cache_entry(self, tmp_path):
        settings = settings_for(tmp_path, ui_agent_cache=True)
        driver = FakeDriver([snapshot("a"), snapshot("b")])
        engine = FakeEngine([Decision(operation="CLICK", target="1", target_element=1,
                                      confidence=0.9), done()])
        runner = Runner(settings, GOAL, "http://x/", checks=[], task="explore", driver=driver,
                        engine=engine, verbose=False, cache=None)
        result = runner.run()

        assert result.status == "done_unverified", \
            "没有断言时 DONE 只能算未验收——这正是探索的语义"
        cache_dir = settings.cache_dir
        assert not cache_dir.exists() or not list(cache_dir.glob("*.json")), \
            "探索一步都不许录制"


class TestSkeleton:
    def test_skeleton_has_a_goal_but_no_checks(self):
        result = write_result("done_unverified")
        text = build_skeleton(GOAL, result)
        assert "checks: []" in text and GOAL in text

    def test_skeleton_is_rejected_by_case_compile(self):
        """故意过不了：逼 agent 自己写断言，不替它编。"""
        result = write_result("done_unverified")
        compiled = compile_case(build_skeleton(GOAL, result))
        assert not compiled["ok"]
        assert {item["code"] for item in compiled["errors"]} == {"checks_empty"}
        assert compiled["plan"] is None

    def test_skeleton_carries_a_stable_id(self):
        result = write_result("done_unverified")
        text = build_skeleton(GOAL, result)
        assert "id: " in text, "骨架要带 id，否则 agent 改措辞就丢录像"

    def test_skeleton_with_agent_written_checks_compiles(self):
        result = write_result("done_unverified")
        text = build_skeleton(GOAL, result).replace(
            "    checks: []",
            "    checks:\n      - text_contains: 票据类型")
        compiled = compile_case(text)
        assert compiled["ok"], compiled["errors"]
        assert compiled["plan"]["case_id"], "id 要透传进 plan"


def write_result(status: str):
    from pathlib import Path

    from ui_agent.run.runner import RunResult
    return RunResult(status, 2, Path("."), "detail")


class TestTrajectory:
    def test_trajectory_lists_actions_by_page(self, tmp_path):
        run = run_explore_once(tmp_path)
        text = (run / "explore" / "trajectory.md").read_text(encoding="utf-8")
        assert "# 探索轨迹" in text and GOAL in text
        assert "## http://x/" in text
        assert "CLICK" in text

    def test_trajectory_never_echoes_var_values(self, tmp_path):
        """--var 的值不落盘：轨迹里只能写「值来自 --var」。"""
        settings = settings_for(tmp_path, ui_agent_cache=True)
        driver = FakeDriver([text_field_snapshot("a"), snapshot("b")])
        engine = FakeEngine([Decision(operation="TYPE_TEXT", target="1", target_element=1,
                                      confidence=0.9), done()])
        run_dir = tmp_path / "run"
        runner = Runner(settings, GOAL, "http://x/", checks=[], task="explore", driver=driver,
                        engine=engine, verbose=False, cache=None, run_dir=run_dir,
                        vars={"账号": "SECRET_VALUE_123"})
        result = runner.run()
        write_artifacts(run_dir, GOAL, result, settings)

        text = (run_dir / "explore" / "trajectory.md").read_text(encoding="utf-8")
        assert "SECRET_VALUE_123" not in text, "轨迹绝不能回显 --var 的值"
        assert "值来自 --var" in text

    def test_secret_fields_are_masked_in_the_trajectory(self, tmp_path):
        settings = settings_for(tmp_path, ui_agent_cache=True)
        secret_field = text_field_snapshot("a")
        secret_field["elements"][0]["secret"] = True
        driver = FakeDriver([secret_field, snapshot("b")])
        engine = FakeEngine([Decision(operation="TYPE_TEXT", target="1", target_element=1,
                                      confidence=0.9), done()])
        run_dir = tmp_path / "run"
        runner = Runner(settings, GOAL, "http://x/", checks=[], task="explore", driver=driver,
                        engine=engine, verbose=False, cache=None, run_dir=run_dir,
                        vars={"账号": "pw"})
        result = runner.run()
        write_artifacts(run_dir, GOAL, result, settings)

        text = (run_dir / "explore" / "trajectory.md").read_text(encoding="utf-8")
        assert "敏感字段（已掩码）" in text and "pw" not in text

    def test_trajectory_handles_an_empty_run(self, tmp_path):
        run_dir = tmp_path / "empty"
        run_dir.mkdir()
        (run_dir / "actions.json").write_text("[]", encoding="utf-8")
        assert "没有产生任何动作" in build_trajectory(run_dir, GOAL)

    def test_trajectory_skips_page_fact_rows(self, tmp_path):
        run = run_explore_once(tmp_path)
        text = (run / "explore" / "trajectory.md").read_text(encoding="utf-8")
        assert "PAGE" not in text, "PAGE 是页面事实，不是动作"


class TestArtifacts:
    def test_writes_the_expected_files(self, tmp_path):
        run = run_explore_once(tmp_path)
        folder = run / "explore"
        assert (folder / "goal.txt").read_text(encoding="utf-8").strip() == GOAL
        assert (folder / "trajectory.md").is_file()
        assert (folder / "explored.yaml").is_file()
        summary = json.loads((folder / "explore.json").read_text(encoding="utf-8"))
        assert summary["goal"] == GOAL and summary["status"] == "done_unverified"

    def test_explore_folder_does_not_break_the_run_layout(self, tmp_path):
        """explore/ 是新增目录，既有产物一个都不许少。"""
        run = run_explore_once(tmp_path)
        for name in ("trace.jsonl", "actions.json", "result.json", "snapshots"):
            assert (run / name).exists(), f"既有产物 {name} 不许丢"

    def test_ask_count_is_zero_without_a_bridge(self, tmp_path):
        run = run_explore_once(tmp_path)
        assert count_asks(run) == 0

    def test_ask_count_reads_the_bridge_folder(self, tmp_path):
        run_dir = tmp_path / "run"
        bridge = run_dir / "bridge"
        bridge.mkdir(parents=True)
        for i in (1, 2, 3):
            (bridge / f"ask-{i:04d}.json").write_text("{}", encoding="utf-8")
        assert count_asks(run_dir) == 3


class TestExploreEntryPoint:
    def test_explore_helper_runs_and_exports(self, tmp_path):
        settings = settings_for(tmp_path, ui_agent_cache=True)
        driver = FakeDriver([snapshot("a"), snapshot("b")])
        engine = FakeEngine([Decision(operation="CLICK", target="1", target_element=1,
                                      confidence=0.9), done()])
        run_dir = tmp_path / "run"

        # 直接注入 driver/engine：explore() 会自建 Runner，这里用 monkeypatch 式的替换
        import ui_agent.run.explore as explore_mod
        original = explore_mod.Runner
        explore_mod.Runner = lambda *a, **k: original(*a, driver=driver, engine=engine, **k)
        try:
            result = explore(settings, GOAL, "http://x/", run_dir)
        finally:
            explore_mod.Runner = original

        assert result.status == "done_unverified"
        assert (run_dir / "explore" / "explored.yaml").is_file()


class TestNoAssertionsMeansUnverified:
    def test_done_without_checks_is_done_unverified(self, tmp_path):
        settings = settings_for(tmp_path)
        driver = FakeDriver([snapshot("a")])
        engine = FakeEngine([done()])
        runner = Runner(settings, GOAL, "http://x/", checks=[], task="explore",
                        driver=driver, engine=engine, verbose=False, cache=None)
        result = runner.run()
        assert result.status == "done_unverified"
        assert "没有配置断言" in result.detail
        assert read_rows(result.run_dir)[0]["kind"] == "run_start"
        _ = (parse_check, sealed_answers)


def run_explore_once(tmp_path):
    settings = settings_for(tmp_path, ui_agent_cache=True)
    driver = FakeDriver([snapshot("a"), snapshot("b")])
    engine = FakeEngine([Decision(operation="CLICK", target="1", target_element=1,
                                  confidence=0.9), done()])
    run_dir = tmp_path / "run"
    runner = Runner(settings, GOAL, "http://x/", checks=[], task="explore", driver=driver,
                    engine=engine, verbose=False, cache=None, run_dir=run_dir)
    result = runner.run()
    write_artifacts(run_dir, GOAL, result, settings)
    return run_dir
