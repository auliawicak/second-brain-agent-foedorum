"""Unit tests for agent.parsing — JSON fence-strip / tool-call extraction."""

from __future__ import annotations

import pytest
from agent.parsing import parse_tool_call


class TestParseToolCall:
    def test_fenced_json(self) -> None:
        text = '```json\n{"tool": "add_task", "args": {"description": "buy milk"}}\n```'
        name, args = parse_tool_call(text)
        assert name == "add_task"
        assert args["description"] == "buy milk"

    def test_bare_json(self) -> None:
        text = '{"tool": "list_tasks", "args": {"status": "pending"}}'
        name, args = parse_tool_call(text)
        assert name == "list_tasks"
        assert args["status"] == "pending"

    def test_repair_trailing_comma(self) -> None:
        text = '{"tool": "save_note", "args": {"content": "hi",}}'
        name, args = parse_tool_call(text)
        assert name == "save_note"
        assert args["content"] == "hi"

    def test_no_tool_returns_none(self) -> None:
        assert parse_tool_call("hello world") is None

    def test_single_quote_repair(self) -> None:
        text = "{'tool': 'get_news', 'args': {}}"
        name, args = parse_tool_call(text)
        assert name == "get_news"

    def test_embedded_in_text(self) -> None:
        text = "Sure! Here is the call:\n```json\n{\"tool\": \"get_current_datetime\", \"args\": {}}\n```\nLet me know."
        name, args = parse_tool_call(text)
        assert name == "get_current_datetime"

    def test_task_id_int(self) -> None:
        text = '{"tool": "complete_task", "args": {"task_id": 3}}'
        name, args = parse_tool_call(text)
        assert name == "complete_task"
        assert args["task_id"] == 3
