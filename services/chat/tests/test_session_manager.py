"""SessionProcessManager: eviction, auto-turn consumer, real spawn lifecycle."""

import asyncio

from session_process import SessionProcessManager


class FakeSP:
    def __init__(self, turns=None, evictable=False):
        self.alive = True
        self._turns = list(turns or [])
        self._evictable = evictable
        self.closed = False

    def is_evictable(self, grace):
        return self._evictable and self.alive

    async def aclose(self):
        self.alive = False
        self.closed = True

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i < len(self._turns):
            t = self._turns[self._i]
            self._i += 1
            return t
        raise StopAsyncIteration


async def _evict_case():
    m = SessionProcessManager()
    a, b = FakeSP(evictable=True), FakeSP(evictable=False)
    m._procs["a"], m._procs["b"] = a, b
    n = await m.evict_idle(0.0)
    assert n == 1, n
    assert a.closed and not b.closed
    assert "a" not in m._procs and "b" in m._procs


async def _auto_consumer_case():
    m = SessionProcessManager()
    calls = []

    async def on_auto(key, turn):
        calls.append((key, turn))

    sp = FakeSP(turns=["T1", "T2"])
    m._procs["s"] = sp
    await m._consume_auto("s", sp, on_auto)  # runs to the process's EOF
    assert calls == [("s", "T1"), ("s", "T2")], calls
    # Process EOF -> dropped so the next user turn respawns + --resumes.
    assert "s" not in m._procs


async def _real_spawn_case():
    # `cat` holds stdin open and stays alive with no output: validates real
    # spawn + get + evict without needing claude.
    m = SessionProcessManager()

    async def noop_auto(key, turn):
        pass

    sp = await m.get_or_create("k", lambda: (["cat"], None), noop_auto)
    assert sp.alive
    assert m.get("k") is sp
    # No turn open, no bg, idle -> evictable immediately at grace 0.
    n = await m.evict_idle(0.0)
    assert n == 1
    assert m.get("k") is None
    await m.aclose_all()


def test_manager():
    asyncio.run(_evict_case())
    asyncio.run(_auto_consumer_case())
    asyncio.run(_real_spawn_case())
    print("PASS: manager eviction + auto-consumer + real spawn lifecycle")


if __name__ == "__main__":
    test_manager()
