import asyncio
import json

import bot_core
from conftest import make_inmem_db


def test_chat_with_tools_appends_sources_and_dispute(monkeypatch):
    db = make_inmem_db()
    bot = bot_core.OneBotClient(db, bot_core.SteamClient(""))

    # Pretend LLM is configured; we will stub streaming.
    bot.llm = object()

    calls = {"n": 0}

    async def fake_llm_chat_stream(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # First turn: tool call.
            return "", [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "search_verified", "arguments": json.dumps({"query": "x"})},
                }
            ]
        # Second turn: final assistant message that forgets sources and dispute.
        return "结论：A。", None

    async def fake_dispatch_tool(name, args, *, steam_id):
        assert name == "search_verified"
        return {
            "ok": True,
            "sources": ["https://example.com/a", "https://example.com/b"],
            "dispute": "多个高可信来源的日期信息不一致，可能存在争议/改动。",
            "evidences": [],
        }

    monkeypatch.setattr(bot, "_llm_chat_stream", fake_llm_chat_stream)
    monkeypatch.setattr(bot, "_dispatch_tool", fake_dispatch_tool)

    out = asyncio.run(bot.chat_with_tools("1", "x", message_type="private"))
    assert out.startswith("存在争议：")
    assert "[来源: https://example.com/a]" in out or "[来源: https://example.com/b]" in out
