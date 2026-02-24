import asyncio

import bot_core
from conftest import CaptureBot, make_inmem_db


class _NoopSteam(bot_core.SteamClient):
    def __init__(self):
        super().__init__(api_key="")


def test_extract_text_from_segments():
    db = make_inmem_db()
    bot = CaptureBot(db, _NoopSteam())
    evt = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 1,
        "message": [
            {"type": "text", "data": {"text": "ab"}},
            {"type": "image", "data": {"file": "x"}},
            {"type": "text", "data": {"text": "cd"}},
        ],
    }
    assert bot._extract_text(evt) == "abcd"


def test_admin_only_bind_command(monkeypatch):
    # Override module-level admin list used by handle_event.
    bot_core.ADMIN_QQ_LIST = {"123"}

    db = make_inmem_db()
    bot = CaptureBot(db, _NoopSteam())

    # Non-admin tries to bind.
    evt = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 999,
        "raw_message": "/bind 999 76561198000000000",
    }
    asyncio.run(bot.handle_event(evt))
    assert bot.sent
    assert "权限不足" in bot.sent[-1]["params"]["message"]

    row = asyncio.run(db.fetchone("SELECT * FROM user_bindings WHERE qq_id=?", ("999",)))
    assert row is None

    # Admin binds.
    evt2 = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 123,
        "raw_message": "/bind 999 76561198000000000",
    }
    asyncio.run(bot.handle_event(evt2))
    assert "绑定成功" in bot.sent[-1]["params"]["message"]
    row2 = asyncio.run(db.fetchone("SELECT * FROM user_bindings WHERE qq_id=?", ("999",)))
    assert row2 is not None
    assert row2["steam_id"] == "76561198000000000"
    assert int(row2["push_enabled"]) == 1
