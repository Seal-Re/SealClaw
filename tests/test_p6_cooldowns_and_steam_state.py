import asyncio

import bot_core
from conftest import make_inmem_db


def test_cooldown_window():
    db = make_inmem_db()
    now = bot_core._now_ts()
    asyncio.run(db.set_cooldown_ts("1", "k", now))
    assert asyncio.run(db.in_cooldown("1", "k", window_sec=12 * 3600, now_ts=now + 1)) is True
    assert asyncio.run(db.in_cooldown("1", "k", window_sec=1, now_ts=now + 2)) is False


def test_steam_state_hash_changes_with_game_id():
    h1 = bot_core._steam_state_hash(persona_state=1, game_id=None, game_name=None)
    h2 = bot_core._steam_state_hash(persona_state=1, game_id="570", game_name="Dota 2")
    assert h1 != h2


def test_upsert_and_get_last_state():
    db = make_inmem_db()
    asyncio.run(
        db.upsert_steam_last_state(
            "1",
            ts=123,
            persona_state=1,
            game_id="570",
            game_name="Dota 2",
            h="abc",
        )
    )
    row = asyncio.run(db.get_steam_last_state("1"))
    assert row is not None
    assert int(row["last_ts"]) == 123
    assert int(row["last_persona_state"]) == 1
    assert row["last_game_id"] == "570"
    assert row["last_game_name"] == "Dota 2"
    assert row["last_hash"] == "abc"
