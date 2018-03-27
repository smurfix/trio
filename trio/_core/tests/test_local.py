import pytest

import threading
import queue

from ... import _core
from ...testing import Sequencer


async def test_local_smoketest():
    for cls in _core.TaskLocal, _core.RunLocal:
        local = cls()

        assert local.__dict__ == {}
        assert vars(local) == {}
        assert dir(local) == []
        assert not hasattr(local, "a")

        local.a = 1
        assert local.a == 1
        assert local.__dict__ == {"a": 1}
        assert vars(local) == {"a": 1}
        assert dir(local) == ["a"]
        assert hasattr(local, "a")

        del local.a

        with pytest.raises(AttributeError):
            local.a
        with pytest.raises(AttributeError):
            del local.a

        assert local.__dict__ == {}
        assert vars(local) == {}

        local.__dict__["b"] = 2
        assert local.b == 2

        async def child():
            assert local.b == 2

        async with _core.open_nursery() as nursery:
            nursery.start_soon(child)


async def test_local_isolation():
    tlocal = _core.TaskLocal()
    rlocal = _core.RunLocal()

    tlocal.a = "task root"
    rlocal.a = "run root"

    seq = Sequencer()

    async def child1():
        async with seq(0):
            assert tlocal.a == "task root"
            assert rlocal.a == "run root"

            tlocal.a = "task child1"
            rlocal.a = "run child1"

        async with seq(2):
            assert tlocal.a == "task child1"
            assert rlocal.a == "run child2"

    async def child2():
        async with seq(1):
            assert tlocal.a == "task root"
            assert rlocal.a == "run child1"

            tlocal.a = "task child2"
            rlocal.a = "run child2"

    async with _core.open_nursery() as nursery:
        nursery.start_soon(child1)
        nursery.start_soon(child2)

    assert tlocal.a == "task root"
    assert rlocal.a == "run child2"


def test_run_local_multiple_runs():
    r = _core.RunLocal()

    async def main(x):
        assert not hasattr(r, "attr")
        r.attr = x
        assert hasattr(r, "attr")
        assert r.attr == x

    # Nothing spills over from one run to the next
    _core.run(main, 1)
    _core.run(main, 2)


def test_run_local_simultaneous_runs():
    r = _core.RunLocal()

    result_q = queue.Queue()

    async def main(x, in_q, out_q):
        in_q.get()
        assert not hasattr(r, "attr")
        r.attr = x
        assert hasattr(r, "attr")
        assert r.attr == x
        out_q.put(None)
        in_q.get()
        assert r.attr == x

    def harness(x, in_q, out_q):
        result_q.put(_core.Result.capture(_core.run, main, x, in_q, out_q))

    in_q1 = queue.Queue()
    out_q1 = queue.Queue()
    t1 = threading.Thread(target=harness, args=(1, in_q1, out_q1))
    t1.start()

    in_q2 = queue.Queue()
    out_q2 = queue.Queue()
    t2 = threading.Thread(target=harness, args=(2, in_q2, out_q2))
    t2.start()

    in_q1.put(None)
    out_q1.get()

    in_q2.put(None)
    out_q2.get()

    in_q1.put(None)
    in_q2.put(None)
    t1.join()
    t2.join()
    result_q.get().unwrap()
    result_q.get().unwrap()

    with pytest.raises(RuntimeError):
        r.attr


def test_local_outside_run():
    for cls in _core.RunLocal, _core.TaskLocal:
        local = cls()

        with pytest.raises(RuntimeError):
            local.a = 1

        with pytest.raises(RuntimeError):
            dir(local)


async def test_local_inheritance_from_spawner_not_supervisor():
    t = _core.TaskLocal()

    t.x = "supervisor"

    async def spawner(nursery):
        t.x = "spawner"
        nursery.start_soon(child)

    async def child():
        assert t.x == "spawner"

    async with _core.open_nursery() as nursery:
        nursery.start_soon(spawner, nursery)


async def test_local_defaults():
    for cls in _core.TaskLocal, _core.RunLocal:
        local = cls(default1=123, default2="abc")
        assert local.default1 == 123
        assert local.default2 == "abc"
        del local.default1
        assert not hasattr(local, "default1")
