import asyncio
import sys

from hastori_alarm.service import run

if __name__ == "__main__":
    if sys.platform == "win32":  # aio-pika and asyncpg listeners work best on the selector loop
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())
