"""
This namespace represents the core functionality that has to be built-in
and deal with private internal data structures. Things in this namespace
are publicly available in either trio, trio.hazmat, or trio.testing.
"""


from ._exceptions import (
    TrioInternalError, RunFinishedError, WouldBlock, Cancelled,
    BusyResourceError, ClosedResourceError, BrokenResourceError, EndOfChannel
)

from ._multierror import MultiError

from ._ki import (
    enable_ki_protection, disable_ki_protection, currently_ki_protected
)

# TODO:  make the _run namespace a lot less magical
from ._run import *

# Has to come after _run to resolve a circular import
from ._traps import (
    cancel_shielded_checkpoint, Abort, wait_task_rescheduled,
    temporarily_detach_coroutine_object, permanently_detach_coroutine_object,
    reattach_detached_coroutine_object
)

from ._entry_queue import TrioToken

from ._parking_lot import ParkingLot

from ._local import RunVar

if hasattr(_run, "wait_readable"):
    wait_socket_readable = wait_readable
    wait_socket_writable = wait_writable
    notify_socket_close = notify_fd_close
