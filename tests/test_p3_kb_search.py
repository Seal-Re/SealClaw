import asyncio

import bot_core
from conftest import make_inmem_db


def test_search_verified_hits_kb_first(monkeypatch):
    db = make_inmem_db()
    db.fts_enabled = False
    bot = bot_core.OneBotClient(db, bot_core.SteamClient(""))

    now = bot_core._now_ts()
    asyncio.run(
        db.upsert_kb_item(
            url="https://example.com/kb",
            title="t",
            snippet="s",
            text="版本 v1.2.3 发布于 2026-02-01",
            media_type="text",
            source="web",
            trust_score=0.9,
            use_case="fact",
            fetched_at=now,
            expires_at=now + 3600,
            content_hash="x",
        )
    )

    out = asyncio.run(bot._dispatch_tool("search_verified", {"query": "v1.2.3"}, steam_id=None))
    assert out["ok"] is True
    assert out["from"] == "kb"
    assert "https://example.com/kb" in (out.get("sources") or [])
