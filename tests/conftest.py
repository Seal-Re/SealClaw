import os
import sys

# Ensure repo root is importable so tests can `import bot_core` when running via `pytest`.
_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import bot_core  # noqa: E402


class CaptureBot(bot_core.OneBotClient):
    """OneBotClient variant that captures outbound actions instead of using a real WS."""

    def __init__(self, db: bot_core.DB, steam: bot_core.SteamClient):
        super().__init__(db, steam)
        self.sent = []

    async def send_action(self, action: str, params: dict) -> None:
        self.sent.append({"action": action, "params": params})


def make_inmem_db() -> bot_core.DB:
    # Uses a single SQLite connection and the same schema as production.
    return bot_core.DB(":memory:")
