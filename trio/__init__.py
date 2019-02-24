"""Trio - Pythonic async I/O for humans and snake people.
"""

# General layout:
#
# trio/_core/... is the self-contained core library. It does various
# shenanigans to export a consistent "core API", but parts of the core API are
# too low-level to be recommended for regular use.
#
# trio/*.py define a set of more usable tools on top of this. They import from
# trio._core and from each other.
#
# This file pulls together the friendly public API, by re-exporting the more
# innocuous bits of the _core API + the higher-level tools from trio/*.py.

from ._version import __version__

from ._core import (
    TrioInternalError, RunFinishedError, WouldBlock, Cancelled,
    BusyResourceError, ClosedResourceError, MultiError, run, open_nursery,
    CancelScope, current_effective_deadline,
    TASK_STATUS_IGNORED, current_time, BrokenResourceError, EndOfChannel
)

from ._timeouts import (
    move_on_at, move_on_after, sleep_forever, sleep_until, sleep, fail_at,
    fail_after, TooSlowError
)

from ._sync import (
    Event, CapacityLimiter, Semaphore, Lock, StrictFIFOLock, Condition
)

from ._threads import (
    run_sync_in_worker_thread, current_default_worker_thread_limiter,
    BlockingTrioPortal
)

from ._highlevel_generic import aclose_forcefully, StapledStream

from ._channel import open_memory_channel

from ._signals import open_signal_receiver

from ._highlevel_socket import SocketStream, SocketListener

import platform
if platform.python_implementation() != "micro":

    from ._file_io import open_file, wrap_file

    from ._path import Path

    from ._subprocess import Process

    # TODO
    from ._ssl import SSLStream, SSLListener, NeedHandshakeError

from ._highlevel_serve_listeners import serve_listeners

from ._highlevel_open_tcp_stream import open_tcp_stream

from ._highlevel_open_tcp_listeners import open_tcp_listeners, serve_tcp

from ._highlevel_open_unix_stream import open_unix_socket

from ._highlevel_ssl_helpers import (
    open_ssl_over_tcp_stream, open_ssl_over_tcp_listeners, serve_ssl_over_tcp
)

# Imported by default
from . import hazmat
from . import socket
from . import abc
# Not imported by default: testing
if False:
    from . import testing

# Having the public path in .__module__ attributes is important for:
# - exception names in printed tracebacks
# - sphinx :show-inheritance:
# - deprecation warnings
# - pickle
# - probably other stuff
from ._util import fixup_module_metadata
fixup_module_metadata(__name__, globals())
fixup_module_metadata(hazmat.__name__, hazmat.__dict__)
fixup_module_metadata(socket.__name__, socket.__dict__)
fixup_module_metadata(abc.__name__, abc.__dict__)
del fixup_module_metadata
