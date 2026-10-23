import asyncio
import threading
import time

from routers.file_history_router import _store_call


def test_history_io_does_not_block_event_loop():
    async def scenario():
        entered=threading.Event()
        release=threading.Event()
        def work():
            entered.set()
            assert release.wait(2)
            return 'done'
        pending=asyncio.create_task(_store_call(work))
        deadline=time.monotonic()+1
        while not entered.is_set():
            assert time.monotonic()<deadline
            await asyncio.sleep(.01)
        release.set()
        assert await pending=='done'
    asyncio.run(scenario())
