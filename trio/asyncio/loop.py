# This code implements a clone of the asyncio mainloop which hooks into
# Trio.

import os
import sys
import trio
import asyncio
import math
import heapq
import signal
import threading
import selectors

import logging
logger = logging.getLogger(__name__)

from functools import partial
from asyncio.events import _format_callback, _get_function_source
from selectors import _BaseSelectorImpl, EVENT_READ, EVENT_WRITE

__all = ['TrioEventLoop']


class _Clear:
    def clear(self):
        pass


class _AddHandle(_Clear):
    __slots__ = ('_loop',)

    def __init__(self, loop):
        self._loop = loop

    def append(self, handle):
        assert self._loop == handle._loop
        handle._loop._q.put_nowait(handle)


_tag = 1


def _next_tag():
    global _tag
    _tag += 1
    return _tag


def _format_callback_source(func, args, kwargs):
    func_repr = _format_callback(func, args, kwargs)
    source = _get_function_source(func)
    if source:
        func_repr += ' at %s:%s' % source
    return func_repr

_nr=0

class _QuitEvent(trio.Event):
    """Used to signal the mainloop to stop,
    as opposed to a "normal" event used for syncing.
    """
    pass


class _TrioHandle:
    """
    This extends asyncio.Handle by providing:
    * a way to pass keyword arguments
    * a way to cancel an async callback
    * a way to declare the type of the callback function
    
    ``is_sync`` may be
    * True: sync function, use _call_sync()
    * False: async function, use _call_async()
    * None: also async, but the callback function accepts
      the handle as its sole argument

    The caller is responsible for checking whether the handle
    has been cancelled before invoking ``call_[a]sync()``.
    """

    def __init__(self,*a,**k):
        global _nr
        _nr = _nr+1
        self._nr = _nr
        super().__init__(*a,**k)

    def _init(self, kwargs, is_sync):
        """Secondary init.
        """
        self._kwargs = kwargs
        self._is_sync = is_sync
        self._scope = None

    def cancel(self):
        super().cancel()
        self._kwargs = None
        if self._scope is not None:
            self._scope.cancel()
        print("H Cancel",self)

    def _cb_future_cancel(self, f):
        """If a Trio task completes an asyncio Future,
        add this callback to the future
        and set ``_scope`` to the Trio cancel scope
        so that the task is terminated when the future gets canceled.

        """
        if f.cancelled():
            self.cancel()

    def _repr_info(self):
        info = ["%s:%s" % (self.__class__.__name__,self._nr)]
        if self._cancelled:
            info.append('cancelled')
        if self._callback is not None:
            info.append(
                _format_callback_source(
                    self._callback, self._args, self._kwargs
                )
            )
        if self._source_traceback:
            frame = self._source_traceback[-1]
            info.append('created at %s:%s' % (frame[0], frame[1]))
        return info

    def _instrument(self, *a):
        self._loop._task._runner.instrument(*a)

    def _call_sync(self):
        assert self._is_sync
        assert not self._cancelled
        
        try:
            res = self._callback(*self._args, **self._kwargs)
        except Exception as exc:
            logger.exception("Sync call %s", self)
            raise
        else:
            logger.debug("Sync result %s: %s", self, repr(res))
            return res
        
    async def _call_async(self):
        logger.debug("Async call: %s", self)
        assert not self._is_sync
        assert not self._cancelled
        try:
            with trio.open_cancel_scope() as scope:
                self._scope = scope
                if self._is_sync is None:
                    res = await self._callback(self)
                else:
                    res = await self._callback(*self._args, **self._kwargs)
        except Exception as exc:
            logger.exception("Async call %s", self)
            raise
        except trio.Cancelled as exc:
            logger.debug("Async call %s: cancelled", self)
            raise
        else:
            logger.debug("Async result %s: %s", self, repr(res))
            return res
        finally:
            self._scope = None


class Handle(_TrioHandle, asyncio.Handle):
    def __init__(self, callback, args, kwargs, loop, is_sync):
        super().__init__(callback, args, loop)
        self._init(kwargs, is_sync)
        print("H New",self)

class DeltaTime:
    __slots__ = ('delta')

    def __init__(self, delta=0):
        self.delta = delta

    def __add__(self, x):
        return DeltaTime(self.delta + x)

    def __iadd__(self, x):
        self.delta += x

    def __sub__(self, x):
        if isinstance(x, DeltaTime):
            return self.delta - x.delta
        return DeltaTime(self.delta - x)

    def __isub__(self, x):
        self.delta -= x


class TimerHandle(_TrioHandle, asyncio.TimerHandle):
    def __init__(
            self, when, callback, args, kwargs, loop, is_sync,
            is_relative=False
    ):
        super().__init__(when, callback, args, loop)
        if isinstance(when, DeltaTime):
            assert not is_relative
            when = when.delta
            is_relative = True
        self._init(kwargs, is_sync)
        self._relative = is_relative
        print("TH New",self)

    def _abs_time(self):
        if self._relative:
            self._when += self._loop.time()
            self._relative = False

    def _rel_time(self):
        if not self._relative:
            self._when -= self._loop.time()
            self._relative = True


class _TrioSelector(_BaseSelectorImpl):
    """A selector that hooks into a ``TrioEventLoop``.

    In fact it's just a basic selector that disables the actual
    ``select()`` method, as that is controlled by the event loop.
    """

    def select(self, timeout=None):
        raise NotImplementedError

    def _select(self, r, w, x, timeout=None):
        raise NotImplementedError


class TrioExecutor:
    def __init__(self, limiter=None):
        self._running = True
        self._limiter = limiter

    async def submit(self, func, *args):
        if not self._running:
            raise RuntimeError("Executor is down")
        return await trio.run_sync_in_worker_thread(
            self._runner, func, *args, limiter=self._limiter
        )

    def shutdown(self, wait=None):
        self._running = False

    def _runner(self, func, *args):
        import os
        try:
            logger.debug("THREAD START %d %s %s", os.getpid(), func, args)
            res = func(*args)
        except BaseException as exc:
            logger.exception("THREAD ERROR %d", os.getpid())
            raise
        else:
            logger.debug("THREAD RES %s %s", os.getpid(), repr(res))
            return res


class TrioEventLoop(asyncio.unix_events._UnixSelectorEventLoop):
    """An asyncio mainloop for trio

    This code implements a semi-efficient way to run asyncio code within Trio.
    """
    _token = None

    def __init__(self):
        # Processing queue
        self._q = trio.Queue(9999)

        # interim storage for calls to ``call_soon_threadsafe``
        # while the main loop is not running
        self._delayed_calls = []

        super().__init__(_TrioSelector())

        # replaced internal data
        self._ready = _AddHandle(self)
        self._scheduled = _Clear()
        self._default_executor = TrioExecutor()

        self._orig_signals = {}

        # we need to do our own timeout handling
        self._timers = []

    def time(self):
        """Trio's idea of the current time.

        Trio returns an error when you try to access the current time when
        the asyncio loop is not running. In that case, this function
        returns a DeltaTime object, which represents the time at which
        the loop is restarted.
        """
        if self._token is None:
            return DeltaTime()
        else:
            return trio.current_time()

    async def wait_for(self, future, _scope=None):
        """Wait for an asyncio future in Trio code.

        Cancellations will be propagated bidirectionally;
        the default for propagating asyncio cancellations
        to Trio is the inner-most cancel scope. If that's
        not what you need, pass in an explicit scope`.
        """
        current_task = trio.hazmat.current_task()
        assert self._task is not current_task

        def is_done(f):
            if f.cancelled():
                _scope.cancel()
                return

            exc = f.exception()
            if exc is None:
                res = trio.hazmat.Value(f.result())
            else:
                res = trio.hazmat.Error(exc)
            trio.hazmat.reschedule(current_task, next_send=res)

        def is_aborted(raise_cancel):
            if not future.cancelled():
                future.remove_done_callback(is_done)
                future.cancel()
            return trio.hazmat.Abort.SUCCEEDED

        future.add_done_callback(is_done)
        if _scope is None:
            _scope = trio.hazmat.current_task()._cancel_stack[-1]
        return await trio.hazmat.wait_task_rescheduled(is_aborted)

    async def call_asyncio(self, p, *a, _scope=None, **k):
        """Call an asyncio function or method from Trio.
        
        Returns/Raises: whatever the procedure does.

        Cancellations will be propagated bidirectionally.
        """
        if _scope is None:
            _scope = trio.hazmat.current_task()._cancel_stack[-1]

        try:
            f = asyncio.ensure_future(p(*a), loop=self)
        except asyncio.CancelledError:
            _scope.cancel()
            await trio.sleep(0)
            raise RuntimeError("cancel didn't kill me")

        return await self.wait_for(f, _scope)

    def call_trio(self, p, *a, **k):
        """Call an asynchronous Trio function from asyncio.

        Returns a Future with the result / exception.

        Cancelling the future will cancel the task running your procedure,
        or prevent it from starting if that is stil possible.
        """
        f = asyncio.Future(loop=self)
        h = Handle(self.__call_trio, (
            f,
            p,
        ) + a, k, self, None)
        self._queue_handle(h)
        f.add_done_callback(h._cb_future_cancel)
        return f

    async def __call_trio(self, h):
        f, proc, *args = h._args
        if f.cancelled():
            return
        try:
            with trio.open_cancel_scope() as scope:
                h._scope = scope
                res = await proc(*args, **h._kwargs)
            if scope.cancelled_caught:
                f.cancel()
                return
        except Exception as exc:
            f.set_exception(exc)
        else:
            f.set_result(res)

    def call_trio_sync(self, p, *a, **k):
        """Call a synchronous function from asyncio.

        Returns a Future with the result / exception.

        Cancelling the future will prevent the code from running,
        assuming that is still possible.

        You might need to use this method if your code needs access to
        features which are only available when Trio is running, such as
        global task-specific variables or the current time.
        (Otherwise you should simply call the code in question directly.)
        """
        f = asyncio.Future(loop=self)
        self._queue_handle(
            Handle(self.__call_trio_sync, (
                f,
                p,
            ) + a, k, self, None)
        )
        return f

    async def __call_trio_sync(self, h):
        f, proc, *args = h._args
        if f.cancelled():
            return
        try:
            res = proc(*args, **h._kwargs)
        except trio.Cancelled:  # should probably never happen, but …
            f.cancel()
        except Exception as exc:
            f.set_exception(exc)
        else:
            f.set_result(res)

    def call_later(self, delay, callback, *args):
        """asyncio's timer-based delay

        Note that the callback is a sync function.
        """
        self._check_callback(callback, 'call_later')
        self._check_closed()
        assert delay >= 0, delay
        h = TimerHandle(delay, callback, args, {}, self, True, True)
        if self._token is None:
            self._delayed_calls.append(h)
        else:
            h = TimerHandle(delay, callback, args, {}, self, True, True)
            self._q.put_nowait(h)
        return h

        h = Handle(self.__call_later, (delay, callback) + args, {}, self, None)
        self._q.put_nowait(h)
        return h

    def _queue_handle(self, handle):
        if self._token is None:
            self._delayed_calls.append(handle)
        else:
            self._q.put_nowait(handle)
        return handle

    def call_at(self, when, callback, *args):
        """asyncio's time-based delay

        Note that the callback is a sync function.
        """
        self._check_callback(callback, 'call_at')
        self._check_closed()
        return self._queue_handle(
            TimerHandle(when, callback, args, {}, self, True)
        )

    def call_soon(self, callback, *args):
        self._check_callback(callback, 'call_soon')
        self._check_closed()
        return self._queue_handle(Handle(callback, args, {}, self, True))

    def call_soon_threadsafe(self, callback, *args):
        self._check_callback(callback, 'call_soon_threadsafe')
        self._check_closed()
        h = Handle(callback, args, {}, self, True)
        if self._token is None:
            self._delayed_calls.append(h)
        else:
            self._token.run_sync_soon(self._q.put_nowait, h)
        return h

    def call_soon_async(self, callback, *args):
        return self._queue_handle(Handle(callback, args, {}, self, False))

    # supersede some built-ins which should not be used

    def _add_callback(self, handle, _via_token=False):
        assert isinstance(handle, Handle), 'A Handle is required here'
        if handle._cancelled:
            return
        assert not isinstance(handle, TimerHandle)

        if via_token:
            self._token.run_sync_soon(self._q.put_nowait, h)
        else:
            self._q.put_nowait(h)

    def _add_callback_signalsafe(self, handle):
        self._add_callback(
            handle, _via_token=(self._task is not trio.hazmat.current_task())
        )

    def _timer_handle_cancelled(self, handle):
        pass

    def run_in_executor(self, executor, func, *args):
        """
        Delegate running a synchronous function to another thread.

        Limitation:
        The executor must be None, or a (subclass of) ``TrioExecutor``.

        Returns an asyncio.Future.
        """
        self._check_callback(func, 'run_in_executor')
        self._check_closed()
        if executor is None:
            executor = self._default_executor
        else:
            assert isinstance(executor, TrioExecutor)
        return self.call_trio(executor.submit, func, *args)

    def _handle_sig(self, sig, _):
        h = self._signal_handlers[sig]
        if self._token is None:
            self._delayed_calls.append(h)
        else:
            self._token.run_sync_soon(self._q.put_nowait, h)

    async def _sync(self):
        w = trio.Event()
        self._q.put_nowait(w)
        await w.wait()

    def add_signal_handler(self, sig, callback, *args):
        self._check_signal(sig)
        self._check_closed()
        if sig == signal.SIGKILL:
            raise RuntimeError("SIGKILL cannot be caught")
        h = Handle(callback, args, {}, self, True)
        assert sig not in self._signal_handlers, "Signal %d is already caught" % (
            sig,
        )
        self._orig_signals[sig] = signal.signal(sig, self._handle_sig)
        self._signal_handlers[sig] = h

    def remove_signal_handler(self, sig):
        self._check_signal(sig)
        try:
            h = self._signal_handlers.pop(sig)
        except KeyError:
            return False
        h.cancel()
        signal.signal(sig, self._orig_signals[sig])
        del self._orig_signals[sig]
        return True

    def _add_reader(self, fd, callback, *args):
        self._check_closed()
        handle = Handle(callback, args, {}, self, True)
        reader = self._set_read_handle(fd, handle)
        if reader is not None:
            print("OR",reader)
            reader.cancel()
        if self._token is None:
            print("DR",handle)
            return
        self._nursery.start_soon(self._reader_loop, fd, handle)
        print("AR",handle)

    def _remove_reader(self, fd):
        if self.is_closed():
            print("R",fd,"closed")
            return False
        try:
            key = self._selector.get_key(fd)
        except KeyError:
            return False
        else:
            mask, (reader, writer) = key.events, key.data
            mask &= ~asyncio.selectors.EVENT_READ
            if not mask:
                print("R",fd,"unreg",reader)
                self._selector.unregister(fd)
            else:
                print("R",fd,"modNR",reader)
                self._selector.modify(fd, mask, (None, writer))

            if reader is not None:
                print("R",fd,"cancel",reader)
                reader.cancel()
                return True
            else:
                print("R",fd,"none")
                return False

    def _set_read_handle(self, fd, handle):
        try:
            key = self._selector.get_key(fd)
        except KeyError:
            self._selector.register(fd, EVENT_READ, (handle, None))
            return None
        else:
            mask, (reader, writer) = key.events, key.data
            self._selector.modify(fd, mask | EVENT_READ, (handle, writer))
            return reader

    async def _reader_loop(self, fd, handle, task_status=trio.STATUS_IGNORED):
        task_status.started()
        self._task._runner.instrument("start_io_task","read",fd,handle)
        with trio.open_cancel_scope() as scope:
            handle._scope = scope
            try:
                while not handle._cancelled:
                    print("RX",handle)
                    await trio.hazmat.wait_readable(fd)
                    handle._call_sync()
                    await self._sync()
            except Exception as exc:
                logger.exception("Reading %d: Calling %s", fd, handle)
            finally:
                handle._scope = None
                self._task._runner.instrument("stop_io_task","read",fd,handle)

    # writing to a file descriptor

    def _add_writer(self, fd, callback, *args):
        self._check_closed()
        handle = Handle(callback, args, {}, self, True)
        writer = self._set_write_handle(fd, handle)
        if writer is not None:
            print("OW",writer)
            writer.cancel()
        if self._token is None:
            print("DW",handle)
            return
        self._nursery.start_soon(self._writer_loop, fd, handle)
        print("AW",handle)

    def _remove_writer(self, fd):
        if self.is_closed():
            print("W",fd,"closed")
            return False
        try:
            key = self._selector.get_key(fd)
        except KeyError:
            return False
        else:
            mask, (reader, writer) = key.events, key.data
            mask &= ~asyncio.selectors.EVENT_WRITE
            if not mask:
                #print("W",fd,"unreg",writer)
                self._selector.unregister(fd)
            else:
                #print("R",fd,"modNR",writer)
                self._selector.modify(fd, mask, (reader, None))

            if writer is not None:
                writer.cancel()
                return True
            else:
                return False

    def _set_write_handle(self, fd, handle):
        try:
            key = self._selector.get_key(fd)
        except KeyError:
            self._selector.register(fd, EVENT_WRITE, (None, handle))
        else:
            mask, (reader, writer) = key.events, key.data
            self._selector.modify(fd, mask | EVENT_WRITE, (reader, handle))
            return writer

    async def _writer_loop(self, fd, handle, task_status=trio.STATUS_IGNORED):
        with trio.open_cancel_scope() as scope:
            handle._scope = scope
            task_status.started()
            self._task._runner.instrument("start_io_task", "write", fd, handle)
            try:
                while not handle._cancelled:
                    print("WX",handle)
                    await trio.hazmat.wait_writable(fd)
                    handle._call_sync()
                    await self._sync()
            except Exception as exc:
                logger.exception("writing %d: Calling %s", fd, handle)
            finally:
                handle._scope = None
                self._task._runner.instrument("stop_io_task","write",fd,handle)

    def _save_fds(self):
        map = self._selector.get_map()
        saved = [{}, {}]
        for fd, key in list(self._selector.get_map().items()):
            for flag in (0, 1):
                if key.events & (1 << flag):
                    handle = key.data[flag]
                    assert handle is not None
                    if not handle._cancelled:
                        if handle._scope is not None:
                            handle._scope.cancel()

    async def _restore_fds(self):
        for fd, key in list(self._selector.get_map().items()):
            for flag in (0, 1):
                if key.events & (1 << flag):
                    handle = key.data[flag]
                    assert handle is not None
                    if not handle._cancelled:
                        if flag:
                            await self._nursery.start(
                                self._writer_loop, fd, handle
                            )
                        else:
                            await self._nursery.start(
                                self._reader_loop, fd, handle
                            )

    # Trio-based main loop

#    def call_exception_handler(self,msg):
#        import pdb;pdb.set_trace()
#        print("OWCH",msg)
#        pass

    async def main_loop(self, task_status=trio.STATUS_IGNORED):
        """This is the Trio replacement of the asyncio loop's main loop.

        Run this method as a standard Trio thread if your main code is
        Trio-based and you need to call asyncio code.

        Use ``run_forever()`` or ``run_until_complete()`` instead if your
        main code is asyncio-based.
        """
        def sig_hook(s,x):
            import pdb;pdb.set_trace()
            pass
        try:
            try:
                _old_intr = signal.signal(signal.SIGINT,sig_hook)
            except ValueError:
                _old_intr = None
            self._task = trio.hazmat.current_task()
            self._task._runner.instrument("loop_start",self)

            async with trio.open_nursery() as nursery:
                self._nursery = nursery
                self._token = trio.hazmat.current_trio_token()
                await self._restore_fds()

                try:
                    for obj in self._delayed_calls:
                        if not getattr(obj, '_cancelled', False):
                            if isinstance(obj, TimerHandle):
                                obj._abs_time()
                                heapq.heappush(self._timers, obj)
                            else:
                                self._q.put_nowait(obj)
                    self._delayed_calls = []
                    task_status.started()

                    time_valid = False
                    while True:
                        obj = None
                        if not time_valid:
                            t = self.time()
                            time_valid = True
                        if not self._timers:
                            timeout = math.inf
                        else:
                            timeout = self._timers[0]._when - t
                            if timeout <= 0:
                                obj = heapq.heappop(self._timers)
                        if obj is None:
                            time_valid = False
                            with trio.move_on_after(timeout) as cancel_scope:
                                obj = await self._q.get()
                            print("run",obj)
                            if cancel_scope.cancel_called:
                                continue

                            if isinstance(obj, trio.Event):
                                if isinstance(obj, _QuitEvent):
                                    break
                                obj.set()
                                continue
                            if isinstance(obj, TimerHandle):
                                obj._abs_time()
                                heapq.heappush(self._timers, obj)
                                continue
                        if obj._cancelled:
                            continue
                        if getattr(obj, '_is_sync', True) is True:
                            try:
                                obj._callback(
                                    *obj._args, **getattr(obj, '_kwargs', {})
                                )
                            except Exception as exc:
                                logger.exception("Calling %s:", repr(obj))
                        else:
                            nursery.start_soon(obj._call_async)
                except Exception as exc:
                    logger.exception("Error in main loop")
                    raise
                except trio.Cancelled:
                    logger.fatal("Mainloop was cancelled directly")
                    raise
                finally:
                    self._task._runner.instrument("loop_end")

                    # Save open file descriptors (but not the self-pipe)
                    try:
                        self._selfpipes = (
                            self._ssock.fileno(), self._csock.fileno()
                        )
                    except AttributeError:
                        self._selfpipe_fds = {}
                    try:
                        self._save_fds()
                    except AttributeError:
                        pass

                    # save timers, by converting them back to relative time
                    for tm in self._timers:
                        tm._rel_time()
                        self._delayed_calls.append(tm)
                    self._timers.clear()

                    del self._nursery
                    self._token = None
                    if _old_intr is not None:
                        signal.signal(signal.SIGINT,_old_intr)
                    if isinstance(obj, trio.Event):
                        obj.set()

        except BaseException as exc:
            print(*trio.format_exception(type(exc), exc, exc.__traceback__))

    def run_task(self, proc, *a, **k):
        """Run a Trio task.

        The asyncio main loop is running in parallel.
        It is stopped when your task finishes.
        """

        if self.is_running():
            raise RuntimeError("This loop is already running")
        f = asyncio.Future(loop=self)
        h = Handle(self.__run_task, (proc, f, a, k), {}, self, False)
        self._delayed_calls.append(h)
        self.run_forever()
        return f.result()

    async def __run_task(self, proc, f, a, k):
        try:
            res = await proc(*a, **k)
        except Exception as exc:
            f.set_exception(exc)
        except trio.Cancelled:
            f.cancel()
            raise
        else:
            f.set_result(res)
        finally:
            self.stop()

    def run_forever(self):
        """Start the main loop

        This method simply runs ``.main_loop()`` as a trio-enabled version
        of the asyncio main loop.

        Use this (usually via ``run_until_complete()``) if your code
        is asyncio-based and you want to use some trio features or
        libraries.

        Use ``main_loop()`` instead if you main code is trio-based.

        Asyncio's ``run_forever()`` does quite a few setup/teardown things.
        Thus, rather than re-implement all of them, our _run_once() method
        actually implements the main event loop instead of just
        single-stepping.
        """
        super().run_forever()

    def _run_once(self):
        trio.run(self.main_loop, instruments=[Tracer()])

    def stop(self):
        """Halt the main loop.

        This returns a trio.Event which will trigger when the loop has
        terminated.
        """
        self._stopping = True
        # reset to False by asyncio's run_forever()

        e = _QuitEvent()
        self._queue_handle(e)
        return e

    def close(self):
        forgot_stop = self.is_running()
        if forgot_stop:
            e = _QuitEvent()
            self._q.put_nowait(e)

        super().close()

        if forgot_stop:
            raise RuntimeError("You need to stop the loop before closing it")


class Tracer(trio.abc.Instrument):
    def before_run(self):
        print("!!! run started")

    def _print_with_task(self, msg, task):
        # repr(task) is perhaps more useful than task.name in general,
        # but in context of a tutorial the extra noise is unhelpful.
        print("{}: {} {}".format(msg, task.name,id(task)))

    def task_spawned(self, task):
        return
        self._print_with_task("### new task spawned", task)

    def task_scheduled(self, task):
        return
        self._print_with_task("### task scheduled", task)

    def before_task_step(self, task):
        return
        self._print_with_task(">>> about to run one step of task", task)

    def after_task_step(self, task):
        return
        self._print_with_task("<<< task step finished", task)

    def task_exited(self, task):
        self._print_with_task("### task exited", task)

    def before_io_wait(self, timeout):
        return
        if timeout:
            print("### waiting for I/O for up to {} seconds".format(timeout))
        else:
            print("### doing a quick check for I/O")
        self._sleep_time = trio.current_time()

    def after_io_wait(self, timeout):
        return
        duration = trio.current_time() - self._sleep_time
        print("### finished I/O check (took {} seconds)".format(duration))

    def after_run(self):
        print("!!! run finished")

    def add_handle(self, dir,fd,handle):
        print("... add {} {} {}".format(dir,fd,handle))

    def remove_handle(self, dir,fd):
        print("... remove {} {} {}".format(dir,fd,handle))

    def start_io_task(self, dir,fd,handle):
        print("... start {} {} {}".format(dir,fd,handle))

    def stop_io_task(self, dir,fd,handle):
        print("... stop {} {} {}".format(dir,fd,handle))

    def loop_start(self,loop):
        print("::: loop start %d %d", os.getpid(),id(loop))

    def loop_end(self):
        print("::: loop end")

    def obj_call(self, obj):
        print("::: {} exec {}".format(obj._nr, obj))

    def obj_result(self, obj, res):
        print("::: {} res {}".format(obj._nr, repr(res)))
        
    def obj_error(self, obj, res):
        print("::: {} exc {}".format(obj._nr, repr(res)))
        
    def obj_cancel(self, obj):
        print("::: {} cancel".format(obj._nr))
        
        
class TrioPolicy(asyncio.unix_events._UnixDefaultEventLoopPolicy):
    _loop_factory = TrioEventLoop


asyncio.set_event_loop_policy(TrioPolicy())
if not isinstance(asyncio.get_event_loop(), TrioEventLoop):  # pragma: no cover
    raise ImportError("You imported trio.asyncio too late.")

