from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import default_settings
from app.runtime import RapidInboxRuntime
from app.smtp.server import SMTPServer


async def main_async() -> None:
    settings = default_settings(Path.cwd())
    runtime = RapidInboxRuntime(settings)
    await runtime.start()
    server = SMTPServer(runtime)
    server.start()
    try:
        await asyncio.Event().wait()
    finally:
        server.stop()
        await runtime.stop()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
