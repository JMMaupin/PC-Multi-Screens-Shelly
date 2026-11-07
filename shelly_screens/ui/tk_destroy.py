"""Hide a Tk window before destroying it (Tk bug 7f05a19378, Windows).

When a toplevel window is destroyed, Tk for Windows first destroys its
native frame, then looks for the window now under the mouse pointer
(`CheckForPointer` in tkWinWm.c). To do so it lists the application's
toplevels in stacking order: it counts those still flagged as mapped --
the dying one included --, allocates an array of that size, and fills it
from what `EnumWindows` returns. The dying window is no longer there, so
the first slot of the array is left uninitialised, and Tk walks the array
from that slot. Whether it crashes depends on what that memory last held.

Seen on 2026-11-07 with 3.1.4 (Tk 9.0.4): adding a Shelly Plug M Gen3
killed the whole application, with nothing in the log. Access violation in
`tcl9tk90!TkWmDeadWindow`, and the stray pointer read `"Ajouter "` -- the
text of the "Add selected" button, freed a moment before. Reproduced from
the sources one time in two; the window rebuild that precedes the close
churns enough memory to make it likely. Fixed upstream on 2026-10-17, in a
Tk newer than the one Python 3.14.7 ships.

The risk is not specific to that dialog: any toplevel destroyed while
another one is shown is exposed. A withdrawn window is no longer flagged
as mapped (`winfo ismapped` drops to 0 at once): it is left out of the
count, and the array gets filled completely. Hence, once installed:

- `Toplevel.destroy` and `Tk.destroy` withdraw the window and every
  toplevel inside it before destroying them -- `simpledialog` included,
  which calls `Toplevel.destroy` directly;
- every window gets a close handler that goes through `destroy`, as the
  title bar's close button would otherwise have Tk destroy the window in
  C, out of Python's reach. A window that sets its own handler replaces
  it; that handler ends up in `destroy` all the same.
"""

from __future__ import annotations

import tkinter as tk

_installed = False


def withdraw_tree(widget: tk.Misc) -> None:
    """Withdraw `widget` if it is a window, and every window inside it."""
    for child in list(widget.children.values()):
        withdraw_tree(child)
    if isinstance(widget, (tk.Tk, tk.Toplevel)):
        try:
            widget.withdraw()
        except tk.TclError:
            pass  # already gone, or no window manager (useTk=False)


def install() -> None:
    """Patch tkinter once for the whole process. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True

    toplevel_init = tk.Toplevel.__init__
    toplevel_destroy = tk.Toplevel.destroy
    root_init = tk.Tk.__init__
    root_destroy = tk.Tk.destroy

    def _default_close(window: tk.Misc) -> None:
        try:
            window.protocol("WM_DELETE_WINDOW", window.destroy)
        except tk.TclError:
            pass

    def init_toplevel(self, *args, **kwargs) -> None:
        toplevel_init(self, *args, **kwargs)
        _default_close(self)

    def destroy_toplevel(self) -> None:
        withdraw_tree(self)
        toplevel_destroy(self)

    def init_root(self, *args, **kwargs) -> None:
        root_init(self, *args, **kwargs)
        _default_close(self)

    def destroy_root(self) -> None:
        withdraw_tree(self)
        root_destroy(self)

    tk.Toplevel.__init__ = init_toplevel
    tk.Toplevel.destroy = destroy_toplevel
    tk.Tk.__init__ = init_root
    tk.Tk.destroy = destroy_root
