"""One GPU lease shared by local model requests on each API event loop."""

import asyncio


def local_model_lock() -> asyncio.Lock:
    # Keep the lease with its event loop, rather than binding a module-global
    # asyncio.Lock to the first loop used by a request or a focused test.
    loop = asyncio.get_running_loop()
    lock = getattr(loop, "_startupforge_local_model_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(loop, "_startupforge_local_model_lock", lock)
    return lock
