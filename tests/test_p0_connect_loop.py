import asyncio

import bot_core


class _FakeWS:
    def __init__(self):
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i == 0:
            self._i += 1
            await asyncio.sleep(0.01)
            # Non-message event; handle_event will ignore.
            return '{"post_type":"meta_event"}'
        raise StopAsyncIteration


class _ConnectCM:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def _run_connect_once(monkeypatch, *, has_additional_headers: bool):
    # Patch module globals used by connect_loop.
    bot_core.WS_URL = "ws://example.invalid"
    bot_core.OB11_ACCESS_TOKEN = "t"

    got = {"kw": None}

    if has_additional_headers:
        def fake_connect(uri, additional_headers=None, **kw):
            got["kw"] = {"additional_headers": additional_headers, **kw}
            return _ConnectCM(_FakeWS())
    else:
        def fake_connect(uri, extra_headers=None, **kw):
            got["kw"] = {"extra_headers": extra_headers, **kw}
            return _ConnectCM(_FakeWS())

    monkeypatch.setattr(bot_core.websockets, "connect", fake_connect)

    db = bot_core.DB(":memory:")
    bot = bot_core.OneBotClient(db, bot_core.SteamClient(""))

    task = asyncio.create_task(bot_core.connect_loop(bot))
    # Wait until connected is set (the connect context is entered).
    for _ in range(200):
        if bot.connected.is_set():
            break
        await asyncio.sleep(0.01)
    assert bot.connected.is_set()

    # Wait until the connection context exits and connect_loop clears the flag.
    for _ in range(200):
        if not bot.connected.is_set():
            break
        await asyncio.sleep(0.01)
    assert bot.connected.is_set() is False

    task.cancel()
    try:
        await task
    except BaseException:
        pass

    return got["kw"], bot


def test_connect_loop_uses_additional_headers(monkeypatch):
    kw, bot = asyncio.run(_run_connect_once(monkeypatch, has_additional_headers=True))
    assert "additional_headers" in kw
    assert kw["additional_headers"] == {"Authorization": "Bearer t"}
    # When the connection context exits, connect_loop clears this in finally.
    assert bot.ws is None
    assert bot.connected.is_set() is False


def test_connect_loop_falls_back_to_extra_headers(monkeypatch):
    kw, bot = asyncio.run(_run_connect_once(monkeypatch, has_additional_headers=False))
    assert "extra_headers" in kw
    assert kw["extra_headers"] == {"Authorization": "Bearer t"}
    assert bot.ws is None
    assert bot.connected.is_set() is False
