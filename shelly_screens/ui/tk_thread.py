"""Tk roots that live and die in their own thread.

Each window runs in its own thread, with its own Tk root, hence its own
Tcl interpreter. Tcl only lets the thread that created an interpreter
delete it: from any other thread, Tcl 8.6 aborts the whole process
("Tcl_AsyncDelete: async handler deleted by the wrong thread"), with no
Python exception and nothing in the log.

Yet the window does not choose the thread: the interpreter goes with the
last Python reference to it. A closed window often stays caught in a
reference cycle (a bound method kept as an attribute is enough), and the
cycle collector frees it later, from whichever thread happens to allocate
at that moment -- the reading thread, or the main thread while the
application quits. Seen on 2026-10-24 with 3.1.1, built on Python 3.14.3:
three crashes in two hours, one of them on quitting.

Python 3.14.7 (gh-83274) leaks the interpreter in that case instead of
deleting it: no crash, but memory lost on every window closed, and the
outcome depends on the Python the executable was built with. `run_window`
settles it: once the window is closed, it collects the cycles itself, in
the window's thread, and the interpreter goes with its last reference,
there and then.

If something still holds the window (a thread still working with it, an
attribute elsewhere), the interpreter cannot go now, and would go later
from the wrong thread: it is then kept for good. A logged leak rather than
a crash.
"""

from __future__ import annotations

import ctypes
import gc
import logging
import tkinter as tk
import types
from typing import Callable, TypeVar

T = TypeVar("T")

log = logging.getLogger("shelly_screens")


def run_window(body: Callable[[tk.Tk], T], on_error: Callable[[Exception], None]) -> T | None:
    """Runs `body` with a new Tk root, then deletes its interpreter here.

    To be called from the window's own thread. `body` builds the window
    and runs its loop; what it returns must not refer to the window. A
    failure goes to `on_error`, and the call then returns None.
    """
    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 - a failed UI must not kill the app
        on_error(exc)
        return None
    interpreter = root.tk
    result = None
    try:
        result = body(root)
    except Exception as exc:  # noqa: BLE001 - a failed UI must not kill the app
        # Handled here, not passed up: its traceback holds the window,
        # which must be gone before the interpreter is released.
        on_error(exc)
    try:
        root.destroy()
    except tk.TclError:
        pass  # already destroyed by the window itself
    del root
    gc.collect()
    holders = [r for r in gc.get_referrers(interpreter)
               if not isinstance(r, types.FrameType)]
    if holders:
        # One extra reference, never returned: the interpreter outlives
        # every holder, and no other thread will ever delete it.
        ctypes.pythonapi.Py_IncRef(ctypes.py_object(interpreter))
        log.info("Closed window still held (%s): its Tcl interpreter is kept",
                 ", ".join(sorted({type(r).__name__ for r in holders})))
    del interpreter  # the last reference: Tcl deletes the interpreter now
    return result
