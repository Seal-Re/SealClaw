import asyncio
import json

import bot_core
from conftest import make_inmem_db


def test_profile_updates_chat_events_and_weights():
    db = make_inmem_db()
    # Use CaptureBot so `handle_event` can reply without needing a real websocket.
    bot = bot_core.OneBotClient(db, bot_core.SteamClient(""))
    bot.connected.set()
    bot.ws = object()  # bypass send_action connection guard; actual send is not asserted here.

    async def _capture_send_action(action: str, params: dict) -> None:
        return None

    bot.send_action = _capture_send_action  # type: ignore[assignment]
    evt = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 1,
        "raw_message": "我最近在玩 艾尔登法环 DLC",
    }
    asyncio.run(bot.handle_event(evt))

    rows = asyncio.run(db.fetchall("SELECT * FROM chat_events WHERE qq_id=?", ("1",)))
    assert len(rows) == 1
    kws = json.loads(rows[0]["keywords_json"])
    assert isinstance(kws, list)
    assert kws

    prof = asyncio.run(db.get_user_profile("1"))
    assert prof is not None
    weights = json.loads(prof["keyword_weights_json"])
    assert isinstance(weights, dict)
    assert weights
