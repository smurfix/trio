import os
import sys
import signal
import threading
import attr
from async_generator import async_generator, yield_, asynccontextmanager

try:
    import signalfd
except ImportError:
    signalfd = None

from . import _core
from . import _sync

__all__ = ["NOT_FOUND"]


def _add_to_all(obj):
    __all__.append(obj.__name__)
    return obj


# Waiting strategies:
#
# - Windows: TODO.
#
# - Linux: signalfd for SIGCHLD, a blocking thread per process, or a signal handler running wait4().
#
# - MacOS/'BSD: kqueue.
#


# Special status / return code for a process that doesn't exist
class NOT_FOUND:
    pass


NOT_FOUND_VALUE = _core.Error(ChildProcessError())

_ChildWatcher = None


class UnknownStatusError(RuntimeError):
    """I can't interpret this exit code."""
    pass


def _compute_returncode(status):
    if status is NOT_FOUND:
        return NOT_FOUND_VALUE
    elif os.WIFSIGNALED(status):
        # The child process died because of a signal.
        return _core.Value(-os.WTERMSIG(status))
    elif os.WIFEXITED(status):
        # The child process exited (e.g sys.exit()).
        return _core.Value(os.WEXITSTATUS(status))
    else:
        # This shouldn't happen.
        return _core.Error(UnknownStatusError(pid, status))


@attr.s(cmp=False)
class _ChildWatcherEntry:
    pid = attr.ib()
    token = attr.ib(default=None)
    event = attr.ib(attr.Factory(_sync.Event))

    result = attr.ib(default=None)
    users = attr.ib(default=0)

    _lock = attr.ib(default=attr.Factory(threading.Lock), repr=False)
    _did_setup = False

    def set(self, status):
        """Remember the result of the subprocess and wake up the waiter,
        if there is one.

        Args:
            pid:
                the process which died
            returncode:
                the process's result code: exit code if >=0, negative
                signal number otherwise.

        This procedure forwards the call to the waiting task's context.
        """
        with self._lock:
            if self.token is None:
                self._set(status)
            else:
                self.token.run_sync_soon(self._set, status)

    async def __aenter__(self):
        do_setup = False
        if self._did_setup is None:
            raise RuntimeError(
                "Re-using %s for %d. Locking problem?" %
                (self.__class__.__name__, self.pid)
            )
        with self._lock:
            self.users += 1
            if not self._did_setup:
                if not self._did_setup:
                    self._did_setup = True
                    do_setup = True
        if do_setup:
            await self.start()
        return self

    async def __aexit__(self, *tb):
        with self._lock:
            assert self.users > 0
            self.users -= 1
            if self.users == 0:
                del _ChildWatcher._data[self.pid]
        if self.users == 0:
            self._did_setup = None
            await self.stop()

    async def start(self):
        """Start watching for this PID.
        
        The default implementation does nothing.
        """
        self.token = _core.current_trio_token()

    async def stop(self):
        """Stop watching for this PID.

        The default implementation does nothing.
        """
        self.token = None

    def _set(self, status):
        """Sets the result to a process's exit status.
        
        Args:
            status:
                Unix: the status as returned by :func:`os.waitpid`.
        """

        if self.result in {None, NOT_FOUND}:
            self.apply_status(status)
        # else: already set, leave alone
        self.event.set()

    def apply_status(self, status):
        """
        Transform the (os-specific) status to a standard return code.

        Args:
            status:
                Unix: the status as returned by :func:`os.waitpid`.

        Result:
            ``self.result`` is set to a :class:`trio.hazmat.Result` which
            encapsulates the return code, or an exception if an error
            occurred.
        """
        if self.result in {None, NOT_FOUND_VALUE}:
            self.result = _compute_returncode(status)


@_add_to_all
class BaseChildWatcher:
    """Base code for "how to wait for child processes to exit".
    
    This class contains common code which should be useable by all
    implementations.
    """

    _sync_ok = False

    _entry_type = None  # override me

    def __init__(self):
        """Set up the watcher.

        Some watchers will not work until you also call their :meth:`init`
        method asynchronously.
        """
        self._lock = threading.Lock()  # to protect _data
        self._data = {}  # pid => ChildWatcherEntry
        self._results = {}  # pid => not-yet-awaited result

        self._attach()

    def _attach(self):
        pass

    async def __aenter__(self):
        """Async part of the watcher's initialization, if required."""
        pass

    async def __aexit__(self, *tb):
        """Close the watcher.

        The default simply calls :meth:`close`, but some watchers may
        require this method to shut down cleanly.
        """
        self.close()

    def close(self):
        """Shut down this watcher."""
        self._detach()

        with self._lock:
            for record in self._data.values():
                if record.result is None:
                    record.result = _core.Error(
                        RuntimeError("Child watcher got cancelled")
                    )
                record.event.set()

    def _detach(self):
        pass

    def check(self):
        """Check if the watcher is still attached / working properly.
        
        This method may silently fix problems if it can do so without
        data loss; otherwise it will raise an error.

        Returns:
            a flag reporting whether everything was OK.

        Raises:
            whatever exception is appropriate.
        """
        return True

    async def scan(self):
        """
        Verify that all waited-for children are still alive.

        Returns:
            the number of children that died unnoticed.

        Note:
            A positive result does not in itself indicate a problem. It may
            also be triggered by a (benign) race condition.
        """
        found = 0
        for pid in list(self._data):
            if not self._waitpid(pid):
                found += 1
        return found

    def _setup_for(self, pid):
        """Set up the data structure necessary to start waiting for a single process,
        and hook that to ``self.data``.

        This method may be entered from multiple threads and thus should
        not be overridden.
        """
        with self._lock:
            record = self._data.get(pid, None)
            if record is not None:
                if record.token != token:
                    raise RuntimeError(
                        "You can't watch a child from multiple Trio loops"
                    )
                record.users += 1
            else:
                self._data[pid] = record = self._entry_type(pid=pid)
        return record

    async def wait_for_child(self, pid):
        """Wait for a child process to end.

        This is the main external entry point.

        Args:
            pid (int): the id of the process to wait for.

        Returns:
            the exit code (>=0), or the negative signal number that killed it.
        """
        async with self._setup_for(pid) as record:
            result = self._results.pop(pid, None)
            if result is not None:
                record._set(result)
            elif record.result is None:
                if record.token != _core.current_trio_token():
                    raise RuntimeError(
                        "You can't watch a child from multiple Trio loops"
                    )
                self._waitpid(pid)
                await record.event.wait()
            return record.result.unwrap()

    def _record(self, pid, status):
        """Record a task's exit status.

        Args:
            pid (int): the process which died
            status: the second result of :func:`os.waitpid`.

        This procedure might be called in any context,
        so it does as little as possible.
        """
        try:
            #os.write(2,b"SIGI RECORD A\n")
            record = self._data[pid]
        except KeyError:
            # may be a zombie, or it is not yet waited for.
            #os.write(2,b"SIGI RECORD S\n")
            self._results[pid] = status
        else:
            #os.write(2,b"SIGI RECORD T\n")
            record.set(status)
            #os.write(2,b"SIGI RECORD U\n")

    def _waitpid(self, pid):
        #os.write(2,b"SIGI WAITPID A %d\n"%pid)
        """Check whether a child process is still alive.
        If it has died since the last check, record its exit status.

        Args:
            pid The process ID of the child to check.

        Returns:
            True if the process is still running.
        """
        assert pid > 0

        try:
            pid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            #os.write(2,b"SIGI WAITPID E\n")
            # The child process may already be reaped
            # (may happen if waitpid() is called elsewhere).
            status = NOT_FOUND
        else:
            #os.write(2,b"SIGI WAITPID R %d %d\n"%(pid,status))
            if pid == 0:
                # The child process is still alive.
                return True

        self._record(pid, status)
        #os.write(2,b"SIGI WAITPID Z\n")
        return False

    @property
    @asynccontextmanager
    @async_generator
    async def async_manager(self):
        """No-op. You can use this watcher without the manager."""
        await yield_(self)


@_add_to_all
class SafeSigChildWatcher(BaseChildWatcher):
    """Attach using SIGCHLD. Poll.
    
    This watcher may be used by programs that don't have a Trio mainloop.

    Warning:
        doing signal handling this way is inherently unsafe.

    This is also the base class for all signal-based implementations.
    """
    _sync_ok = True
    _entry_type = _ChildWatcherEntry

    _orig_sig = None

    def check(self):
        """Check if the watcher is still attached / working properly.
        
        Raises:
            whatever exception is appropriate.
        """

        current_sig = signal.signal(signal.SIGCHLD, self._signal_handler)
        if current_sig != self._signal_handler:
            self._signal_handler()  # run the thing
            if False:
                raise RuntimeError(
                    "%s: SIGCHLD handler was %s" %
                    (self.__class__.__name__, repr(current_sig))
                )

    def _attach(self):
        """Attach the signal handler.

        Override to do nothing if your watcher is not signal-based.
        """
        #os.write(2,b"SIGI ATTA A\n")
        self._orig_sig = signal.signal(signal.SIGCHLD, self._signal_handler)

    def _detach(self):
        """Detach the signal handler.

        Override to do nothing if your watcher is not signal-based.
        """
        #os.write(2,b"SIGI DETA A\n")
        signal.signal(signal.SIGCHLD, self._orig_sig)

    def _signal_handler(self, *args):
        #os.write(2,b"SIGI HAND A\n")
        self._process_signal()
        #os.write(2,b"SIGI HAND Z\n")

    def _process_signal(self):
        for pid in list(self._data):
            self._waitpid(pid)


@_add_to_all
class FastSigChildWatcher(SafeSigChildWatcher):
    """
    A child watcher that doesn't poll each subprocess individually.

    Warning:
        If some other part of your code (or a library you use) eats exit
        codes, code waiting for a process to exit may wait forever.

    Warning:
        doing signal handling this way is inherently unsafe.
    """

    def _process_signal(self):
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                # No more child processes exist.
                return
            else:
                if pid == 0:
                    # At least one child process is still alive.
                    return

                self._record(pid, status)


class _TaskWatcher:
    """
    A mix-in which supplies a task context to the signal handler
    so that :meth:`_process_signal` runs in that task's context
    instead of the signal handler's, whatever that may happen to be.

    This makes handling SIGCHLD a lot safer.

    """
    _nursery = None
    _sync_ok = False

    @property
    @asynccontextmanager
    @async_generator
    async def async_manager(self):
        assert self._nursery is None

        async with _core.open_nursery() as nursery:
            try:
                self._nursery = nursery
                self._token = _core.current_trio_token()
                self._stop = _sync.Event()
                await self._nursery.start(self._processor)

                await yield_(self)

            finally:
                self._stop.set()
                self._token = None
                self._nursery = None

    async def _processor(self, task_status=_core.TASK_STATUS_IGNORED):
        try:
            task_status.started()
            await self._stop.wait()
        finally:
            self._token = None

    def _signal_handler(self, *args):
        self._token.run_sync_soon(self._process_signal)


@_add_to_all
class FastTaskSigChildWatcher(_TaskWatcher, FastSigChildWatcher):
    """Attach using SIGCHLD. Poll.
    
    This watcher can only be used by programs that have a Trio mainloop.

    Warning:
        If some other part of your code (or a library you use) eats exit
        codes, code waiting for a process to exit may wait forever.
    """
    pass


@_add_to_all
class SafeTaskSigChildWatcher(_TaskWatcher, SafeSigChildWatcher):
    """Attach using SIGCHLD. Poll.
    
    This watcher can only be used by programs that have a Trio mainloop.

    Warning:
        Waiting for a large number of processes incurs a performance penalty.
    """
    pass


# TODO
#class _SignalFdWatcher(_TaskWatcher):
#    """
#    A mix-in which uses signalfd() to read subprocess exit status.
#
#    Only available on Linux.
#    """


@_add_to_all
def child_watcher(
    watcher_cls=None, *args, sync=False, _replace=False, **kwargs
):
    """Set the class that's to be used for watching child processes.

    There can only be one child watcher per program. Subsequent calls
    to :function:`child_watcher` will have no effect and return ``None``.

    A default watcher will be instantiated upon the first call to
    :function:`wait_for_child`.

    Args:
        watcher_cls:
            The class to use
        sync:
            Flag to only allow watchers which do not require a Trio loop.

            If you do not set this flag, it might be necessary to run
            your entire program within a ``async with child_watcher()``
            loop. This mode is compatible with all watcher implementations.

        _replace:
            Flag to allow replacing an existing child watcher.
            This must only be used for debugging and testing.
            Otherwise, data loss may result.

        Other arguments:
            All other arguments are passed to ``watcher_cls``.

    Returns:
        The newly-created Watcher object.

    Examples:
    
        Async handler, as part of the Trio mainloop::

            async def main():
                async with child_watcher(FastTaskedChildWatcher, _replace=True).async_manager:
                    await real_main()

        Starting up a synchronous or asyncio-using program::

            def main():
                child_watcher(FastSigChildWatcher, sync=True)
                ...

    """
    global _ChildWatcher

    if _ChildWatcher is not None:
        if not _replace:
            return None
        _ChildWatcher.close()

    if watcher_cls is None:
        watcher_cls = SafeSigChildWatcher

    if sync and not watcher_cls._sync_ok:
        raise RuntimeError(
            "'%s' requires a Trio context." % watcher_cls.__name__
        )

    _ChildWatcher = watcher_cls(*args, **kwargs)
    return _ChildWatcher


@_add_to_all
async def wait_for_child(pid):
    """Wait for a specific child process to end.

    Args:
        pid:
            the id of the process to wait for.

    Returns:
        the exit code (>=0), or the negative signal number that killed it.

    """
    if _ChildWatcher is None:
        await child_watcher(sync=True)
    return await _ChildWatcher.wait_for_child(pid)


@_add_to_all
def check_child_watcher():
    """
    Check if a child watcher is installed and whether it is active.
    
    Returns:
        A boolean; if False, :function:`child_watcher()` will be effective.

    Raises:
        RuntimeError or similar, if a problem is identified.
    """
    if _ChildWatcher is None:
        return False
    _ChildWatcher.check()
    return True


@_add_to_all
async def scan_children():
    """
    Scan all child processes to find waiters whose signal has been missed.
    
    You might want to do this periodically, just to be on the safe side.

    Returns:
        The number of problem processes that have been found.
    """
    if _ChildWatcher is None:
        return False
    return await _ChildWatcher.scan()
