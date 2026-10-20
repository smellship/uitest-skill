"""Agent 桥的文件协议：命名、原子写、坏 JSON、结构校验、附件默认空——不联网、不真睡。"""

from __future__ import annotations

import json

import pytest

from ui_agent.bridge.protocol import (
    KINDS,
    STATES,
    Answer,
    Ask,
    BridgeError,
    BridgeStatus,
    ans_name,
    ask_name,
    parse_answer,
    parse_ask,
    parse_status,
    read_json,
    write_json,
)


class TestNaming:
    def test_file_names_are_zero_padded(self):
        assert ask_name(7) == "ask-0007.json"
        assert ans_name(7) == "ans-0007.json"
        assert ask_name(12345) == "ask-12345.json"

    def test_kinds_and_states_are_closed_vocabularies(self):
        assert KINDS == ("supervise", "decide", "text")
        assert STATES == ("running", "finished", "crashed")


class TestAtomicReadWrite:
    def test_round_trip_is_utf8_and_keeps_chinese(self, tmp_path):
        path = tmp_path / "ask-0001.json"
        write_json(path, {"id": 1, "kind": "supervise", "payload": {"hint": "先展开高级查询"}})
        raw = path.read_text(encoding="utf-8")
        assert "先展开高级查询" in raw, "ensure_ascii=False：中文要原样落盘，便于人读"
        assert read_json(path)["kind"] == "supervise"

    def test_no_temp_file_is_left_behind(self, tmp_path):
        path = tmp_path / "ask-0001.json"
        write_json(path, {"id": 1})
        assert [p.name for p in tmp_path.iterdir()] == ["ask-0001.json"]

    def test_write_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "bridge" / "ask-0001.json"
        write_json(path, {"id": 1})
        assert path.is_file()

    def test_missing_file_reads_as_none(self, tmp_path):
        assert read_json(tmp_path / "ans-0009.json") is None

    def test_broken_json_raises_bridge_error(self, tmp_path):
        path = tmp_path / "ans-0001.json"
        path.write_text('{"id": 1, "answer":', encoding="utf-8")
        with pytest.raises(BridgeError, match="不是合法 JSON"):
            read_json(path)

    def test_top_level_must_be_an_object(self, tmp_path):
        path = tmp_path / "ans-0001.json"
        path.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(BridgeError, match="顶层必须是对象"):
            read_json(path)


class TestContracts:
    def test_ask_defaults(self):
        ask = Ask(id=3, kind="text")
        assert ask.deadline_s == 180.0 and ask.payload == {}

    def test_answer_defaults(self):
        answer = Answer(id=3, answer={"text": "x"})
        assert answer.answered_by == "agent" and answer.note == ""

    def test_status_defaults_to_running(self):
        status = BridgeStatus()
        assert status.state == "running" and status.seq == 0

    def test_attachments_are_empty_in_v1(self, tmp_path):
        """协议预留 payload.attachments，v1 恒为空：未来放开截图只改填充，不改协议。"""
        path = tmp_path / "ask-0001.json"
        write_json(path, {"id": 1, "kind": "supervise", "payload": {"attachments": []}})
        assert parse_ask(read_json(path), path).payload["attachments"] == []

    def test_deadline_accepts_int_or_float(self, tmp_path):
        for value in (600, 600.0):
            path = tmp_path / "ask-0001.json"
            write_json(path, {"id": 1, "kind": "decide", "deadline_s": value})
            assert parse_ask(read_json(path), path).deadline_s == 600.0


class TestParsing:
    def test_unknown_kind_is_rejected(self, tmp_path):
        path = tmp_path / "ask-0001.json"
        with pytest.raises(BridgeError, match="kind 不在"):
            parse_ask({"id": 1, "kind": "teleport"}, path)

    def test_malformed_ask_is_rejected(self, tmp_path):
        path = tmp_path / "ask-0001.json"
        with pytest.raises(BridgeError, match="ask 结构不合法"):
            parse_ask({"kind": "decide"}, path)   # 缺 id

    def test_answer_needs_an_answer_object(self, tmp_path):
        path = tmp_path / "ans-0001.json"
        with pytest.raises(BridgeError, match="ans 结构不合法"):
            parse_answer({"id": 1}, path)

    def test_status_state_is_not_policed_here(self, tmp_path):
        """状态值由使用者解释：解析层只保证结构与类型（未知态按 running 兜底）。"""
        path = tmp_path / "status.json"
        status = parse_status({"state": "running", "seq": 4}, path)
        assert status.seq == 4

    def test_json_dump_is_indented_for_humans(self, tmp_path):
        path = tmp_path / "ans-0001.json"
        write_json(path, {"id": 1, "answer": {"text": "x"}})
        assert json.loads(path.read_text(encoding="utf-8"))["answer"] == {"text": "x"}
        assert "\n" in path.read_text(encoding="utf-8"), "给人看的文件要缩进"
