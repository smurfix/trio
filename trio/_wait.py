import os
import sys
import signal
import threading
from contextlib import contextmanager

try:
    import signalfd
except ImportError:
    signalfd = None

from . import _core
from . import _sync
from ._util import signal_raise, aiter_compat

__all__ = ["NOT_FOUND"]

def _add_to_all(obj):
    __all__.append(obj.__name__)
    return obj

# Waiting strategies:
#
# - Windows: TODO.
#
# - Linux: signalfd for SIGCHLD, or a signal handler running wait4().
#
# - MacOS/'BSD: kqueue.
# 

# Special return code for a process that doesn't exist
NOT_FOUND = 255

_ChildWatcher = None

class UnknownStatus(RuntimeError):
    """I can't interpret this exit code."""
    pass

def _compute_returncode(status):
    if os.WIFSIGNALED(status):
        # The child process died because of a signal.
        return -os.WTERMSIG(status)
    elif os.WIFEXITED(status):
        # The child process exited (e.g sys.exit()).
        return os.WEXITSTATUS(status)
    else:
        # This shouldn't happen.
        raise UnknownStatus(pid, status)

@_add_to_all
class SafeChildWatcher:
    """Attach using SIGCHLD. Poll.
    
    This watcher may be used by programs that don't have a Trio mainloop.

    Warning: doing signal handling this way is inherently unsafe.
    """
    _orig_sig = None

    def __init__(self, *args, **kwargs):
        self._callbacks = {}
        self._results = {}
        self._attach()

    async def init(self):
        """Async part of the watcher's initialization, if required."""
        pass

    async def aclose(self):
        self.close()

    def close(self):
        """Shut down this watcher."""
        signal.signal(signal.SIGCHLD, self._orig_sig)
        for pid,t_e in self._callbacks.items():
            token, event = t_e
            event.set()
            # This will result in a KeyError in wait_for_child()
            # which is mostly what we want

    async def check(self):
        """Check if the watcher is still attached.
        
        You should call this method periodically if you suspect that things
        get lost, e.g. 
        """
        current_sig = signal.signal(signal.SIGCHLD, self._signal_handler)
        if current_sig != self._signal_handler:
            self._signal_handler() # bah
            raise RuntimeError("%s: SIGCHLD handler was %s" % (self.__class__.__name__, repr(current_sig)))

    async def scan(self):
        """
        Verify that all waited-for children are still alive.

        Call this if you suspect that some processes have not been waited
        for, or periodically if you want to be sure.
        """
        for pid in list(self._callbacks):
            self._waitpid(pid)

    def _attach(self):
        self._orig_sig = signal.signal(signal.SIGCHLD, self._signal_handler)
        self._callbacks = {} # pid => loop,proc,args

    def _add_child_handler(self, pid, callback, *args):
        token = _core.current_trio_token()
        self._callbacks[pid] = (token, callback, args)

    def _remove_child_handler(self):
        if pid not in self._callbacks:
            return False
        del self._callbacks[pid]
        return True

    async def wait_for_child(self, pid):
        """Wait for a child process to end.

        :param pid: the id of the process to wait for.
        :return: the exit code (>=0), or the negative signal number that
                 killed it.
        """
        if pid in self._callbacks:
            raise RuntimeError("Somebody is already waiting for this process to end")

        token = _core.current_trio_token()
        event = _sync.Event()
        self._callbacks[pid] = (token,event)
        try:
            if pid not in self._results:
                self._waitpid(pid)
                await event.wait()
            return self._results.pop(pid)
        finally:
            self._callbacks.pop(pid, None) # ignore errors

    def _set(self, pid, resultcode):
        """Remember the result of the subprocess and wake up the waiter,
        if there is one.

        :param pid: the process which died
        :param resultcode: the process's result code: exit code if >=0,
                           negative signal number otherwise.

        This procedure is called in the waiting task's context.
        """

        if self._results.get(pid, NOT_FOUND) == NOT_FOUND:
            self._results[pid] = resultcode
        try:
            token, event = self._callbacks[pid]
        except KeyError:
            return
        else:
            event.set()

    def _trigger(self, pid, returncode):
        """Tell the task waiting for a process its result.

        :param pid: the process which died
        :param resultcode: the process's result code: exit code if >=0,
                           negative signal number otherwise.

        This procedure may be called in signal context.
        """
        try:
            token, event = self._callbacks[pid]
        except KeyError:
            # may be a zombie, or it is not yet waited for.
            # WARNING, the next lines are inherently unsafe
            if self._results.get(pid, NOT_FOUND) == NOT_FOUND:
                self._results[pid] = returncode
        else:
            token.run_sync_soon(self._set, pid, returncode)

    def _signal_handler(self, *args):
        self._process_signal()

    def _process_signal(self):
        for pid in list(self._callbacks):
            self._waitpid(pid)

    def _waitpid(self, pid):
        assert pid > 0

        try:
            pid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            # The child process may already be reaped
            # (may happen if waitpid() is called elsewhere).
            returncode = NOT_FOUND
        else:
            if pid == 0:
                # The child process is still alive.
                return
            returncode = _compute_returncode(status)

        self._trigger(pid, returncode)


@_add_to_all
class FastChildWatcher(SafeChildWatcher):
    """
    A child watcher that doesn't poll each task.

    Warning: If some other part of your code (or a library you use) eats
    exit codes, code waiting for a process to exit may wait forever.

    Warning: doing signal handling this way is inherently unsafe.
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

                returncode = _compute_returncode(status)
                self._trigger(pid, returncode)


class _TaskedWatcher:
    """
    A mix-in which supplies a task context to the signal handler
    so that :meth:`_process_signal` runs in that task's context
    instead of the signal handler's, whatever that may happen to be.

    This makes handling SIGCHLD a lot safer.

    Usage: Supply a ``nursery`` parameter to :func:`set_child_watcher`.
    """
    def __init__(self, *args, nursery=None, **kwargs):
        if nursery is None:
            raise RuntimeError("%s: you need to supply a nursery" % self.__class__.__name__)
        super().__init__(*args, **kwargs)
        self._nursery = nursery

    async def init(self):
        await super().init()
        self._stop = _sync.Event()
        await self._nursery.start(self._processor)

    def close(self):
        self._stop.set()
        super().close()

    async def _processor(self, task_status=_core.TASK_STATUS_IGNORED):
        try:
            self._token = _core.current_trio_token()
            task_status.started()
            await self._stop.wait()
        finally:
            self._token = None

    def _signal_handler(self, *args):
        self._token.run_sync_soon(self._process_signal)

@_add_to_all
class FastTaskedChildWatcher(_TaskedWatcher, FastChildWatcher):
    """Attach using SIGCHLD. Poll.
    
    This watcher can only be used by programs that have a Trio mainloop.

    Warning: If some other part of your code (or a library you use) eats
    exit codes, code waiting for a process to exit may wait forever.

    Usage: Supply a ``nursery`` parameter to :func:`set_child_watcher`.
    """
    pass

@_add_to_all
class SafeTaskedChildWatcher(_TaskedWatcher, SafeChildWatcher):
    """Attach using SIGCHLD. Poll.
    
    This watcher can only be used by programs that have a Trio mainloop.

    Warning: Waiting for a large number of processes incurs a performance penalty.

    Usage: Supply a ``nursery`` parameter to :func:`set_child_watcher`.
    """
    pass

# TODO
#class _SignalFdWatcher(_TaskedWatcher):
#    """
#    A mix-in which uses signalfd() to read subprocess exit status.
#
#    Only available on Linux.
#    """

@_add_to_all
async def set_child_watcher(watcher_cls=None, *args, _replace=False, **kwargs):
    """Set the class that's to be used for watching child processes.

    :param watcher_cls: The class 
    :param _replace: allow replacing an existing child watcher.
                     Should only used for debugging and testing.
    """
    global _ChildWatcher

    if _ChildWatcher is not None:
        if not _replace:
            return False
        await _ChildWatcher.aclose()

    if watcher_cls is None:
        watcher_cls = SafeChildWatcher

    _ChildWatcher = watcher_cls(*args, **kwargs)
    await _ChildWatcher.init()
    return True

@_add_to_all
async def wait_for_child(pid):
    """Wait for a specific child process to end.

    :param pid: the id of the process to wait for.
    :return: the exit code (>=0), or the negative signal number that
             killed it.
    """
    if _ChildWatcher is None:
        await set_child_watcher()
    return await _ChildWatcher.wait_for_child(pid)

