#!/usr/bin/env micropython

import trio
async def bar(s):
    await trio.sleep(s)
    print("Slept",s)
async def foo():
    async with trio.open_nursery() as n:
        n.start_soon(bar,0.1)
        n.start_soon(bar,1.2)
        n.start_soon(bar,2.3)
trio.run(foo)
