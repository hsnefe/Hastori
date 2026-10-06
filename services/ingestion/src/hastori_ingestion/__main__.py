import asyncio
import sys

from hastori_ingestion.service import run

if __name__ == "__main__":
    if sys.platform == "win32":  # aiomqtt needs add_reader, which the default Proactor loop lacks
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())
