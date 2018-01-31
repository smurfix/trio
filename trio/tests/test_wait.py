import os
import time
import itertools

import pytest

from .. import _core
from .. import _wait

from .._wait import *


def fork_sleep(timer=0):
    pid = os.spawnlp(
        os.P_NOWAIT, "sleep", "sleep",
        str(timer) if timer > 0 else "0"
    )
    if timer < 0:
        time.sleep(-timer)
    return pid


def fork_error():
    pid = os.spawnlp(os.P_NOWAIT, "false", "false")
    return pid


# TODO: there's more to life than Unix
known_watchers = [
    SafeSigChildWatcher, SafeTaskSigChildWatcher, FastSigChildWatcher,
    FastTaskSigChildWatcher
]
timers = (-0.1, -0.01, -0.001, 0, 0.001, 0.01, 0.1)


def idfn(test_mode):
    Watcher, timer = test_mode
    return "%s/%s" % (Watcher.__name__, timer)


@pytest.fixture(params=itertools.product(known_watchers, timers), ids=idfn)
def test_mode(request):
    yield request.param


@pytest.fixture(params=known_watchers, ids=lambda _: _.__name__)
def watcher_cls(request):
    yield request.param


async def test_sync_flags():
    assert SafeSigChildWatcher._sync_ok
    assert not SafeTaskSigChildWatcher._sync_ok
    assert FastSigChildWatcher._sync_ok
    assert not FastTaskSigChildWatcher._sync_ok

    with pytest.raises(RuntimeError):
        child_watcher(FastTaskSigChildWatcher, _replace=True, sync=True)
    with pytest.raises(RuntimeError):
        child_watcher(SafeTaskSigChildWatcher, _replace=True, sync=True)


async def test_wait_delayed(test_mode):
    watcher_cls, timer = test_mode
    async with child_watcher(
        watcher_cls, _replace=True
    ).async_manager as watcher:
        pid = fork_sleep(timer)
        res = await wait_for_child(pid)
        assert res == 0
        assert not watcher._data
        assert not watcher._results
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)


async def test_wait_error(watcher_cls):
    async with child_watcher(
        watcher_cls, _replace=True
    ).async_manager as watcher:
        pid = fork_error()
        res = await wait_for_child(pid)
        assert res == 1
        assert not watcher._data
        assert not watcher._results
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
