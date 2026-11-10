"""Structured ownership for collectors whose original exceptions must stay intact."""
import asyncio


async def cancel_and_drain(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    drain = asyncio.gather(*tasks, return_exceptions=True)
    interrupted = False
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError:
            interrupted = True
    drain.result()
    if interrupted:
        raise asyncio.CancelledError


async def gather_owned(*work):
    tasks = [asyncio.create_task(item) for item in work]
    try:
        return await asyncio.gather(*tasks)
    finally:
        await cancel_and_drain(tasks)


async def run_thread_owned(operation, *args, **kwargs):
    """Keep ownership until synchronous I/O settles, even after repeated stop signals."""
    worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise
