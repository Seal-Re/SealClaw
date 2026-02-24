import asyncio

import bot_core
from conftest import make_inmem_db


def test_fetch_bilibili_subtitles_degrades_on_empty_bvid():
    out = asyncio.run(bot_core._fetch_bilibili_subtitles(""))
    assert out == "无法获取视频字幕"


def test_ingest_bilibili_video_degrades_on_block(monkeypatch):
    db = make_inmem_db()
    bot = bot_core.OneBotClient(db, bot_core.SteamClient(""))
    bot.llm = object()

    async def fake_view(bvid: str):
        return {"ok": False, "status": 403, "error": "bilibili blocked"}

    monkeypatch.setattr(bot_core, "_fetch_bilibili_view", fake_view)

    out = asyncio.run(bot.ingest_bilibili_video("BV1xxxx"))
    assert out["ok"] is False
    assert out["error"] == "bilibili blocked"
    assert int(out.get("status") or 0) == 403
