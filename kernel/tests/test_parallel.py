"""多开能力：并发进程之间的目录与 profile 隔离（纯离线，不起浏览器）。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from ui_agent.config import Settings
from ui_agent.driver.playwright_ import LOCK_HINTS, profile_lock_error
from ui_agent.run.dirs import claim_dir, make_run_dir, slugify


class TestSlug:
    def test_keeps_chinese_digits_and_dash(self):
        assert slugify("本地-夹具 2026") == "本地-夹具_2026"

    def test_collapses_punctuation_and_edges(self):
        assert slugify("  登录/查询:*  ") == "登录_查询"

    def test_empty_falls_back_to_task(self):
        assert slugify("///") == "task"

    def test_long_names_are_truncated(self):
        assert len(slugify("用例" * 80)) == 60


class TestClaimDir:
    def test_creates_the_directory(self, tmp_path):
        path = claim_dir(tmp_path / "run")
        assert path.is_dir()
        assert path.name == "run"

    def test_second_claim_gets_a_suffix(self, tmp_path):
        first = claim_dir(tmp_path / "run")
        second = claim_dir(tmp_path / "run")
        assert first != second
        assert second.name == "run-2"
        assert second.is_dir()

    def test_suffixed_directories_do_not_get_reused(self, tmp_path):
        names = [claim_dir(tmp_path / "run").name for _ in range(4)]
        assert names == ["run", "run-2", "run-3", "run-4"]

    def test_concurrent_claimers_never_share_a_directory(self, tmp_path):
        # mkdir 是原子的：十个线程同时抢同一个 base，抢到的路径必须两两不同
        seen: list[str] = []
        lock = threading.Lock()

        def claim() -> None:
            path = claim_dir(tmp_path / "run")
            with lock:
                seen.append(str(path))

        threads = [threading.Thread(target=claim) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(set(seen)) == 10
        assert all(Path(name).is_dir() for name in seen)


class TestMakeRunDir:
    def test_name_is_slug_plus_second_stamp(self, tmp_path):
        path = make_run_dir(tmp_path, "登录 用例")
        assert path.parent == tmp_path
        assert path.name.startswith("登录_用例-")
        assert len(path.name.rsplit("-", 1)[1]) == 6  # HHMMSS
        assert path.is_dir()

    def test_same_second_same_slug_does_not_collide(self, tmp_path):
        first = make_run_dir(tmp_path, "case")
        second = make_run_dir(tmp_path, "case")
        assert first != second


class TestProfileDir:
    def test_flag_beats_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UI_AGENT_PROFILE_DIR", str(tmp_path / "env"))
        settings = Settings(ui_agent_profile_dir=str(tmp_path / "flag"))
        assert settings.profile_dir == tmp_path / "flag"

    def test_two_lanes_get_two_directories(self, tmp_path):
        a = Settings(ui_agent_profile_dir=str(tmp_path / "lane-a")).profile_dir
        b = Settings(ui_agent_profile_dir=str(tmp_path / "lane-b")).profile_dir
        assert a != b


class TestLockDetection:
    @pytest.mark.parametrize("hint", LOCK_HINTS)
    def test_known_signatures_are_recognised(self, hint):
        exc = RuntimeError(f"BrowserType.launch_persistent_context: {hint}")
        assert profile_lock_error(exc)

    def test_other_failures_are_not_mistaken_for_locks(self):
        assert not profile_lock_error(RuntimeError("net::ERR_NAME_NOT_RESOLVED"))
