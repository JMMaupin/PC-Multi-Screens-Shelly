"""The small windows of installation and removal.

Shown before the application exists -- no icon, no settings window yet --,
each runs its own short Tk loop and returns the choice made.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme as theme_module
from .. import __version__, product
from ..i18n import t
from ..win import icon as icon_module


def _window(title: str) -> tuple[tk.Tk, ttk.Frame]:
    root = tk.Tk()
    root.title(title)
    icon_module.apply_to_window(root)
    theme_module.apply(root, "system")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=20)
    frame.pack(fill="both", expand=True)
    photo = icon_module.load_photo(64, master=root)
    if photo is not None:
        label = ttk.Label(frame, image=photo)
        label.image = photo  # keep it alive
        label.pack(side="left", anchor="n", padx=(0, 16))
    return root, frame


def _center(root: tk.Tk) -> None:
    root.update_idletasks()
    width, height = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - width) // 2
    y = (root.winfo_screenheight() - height) // 3
    root.geometry(f"+{x}+{y}")
    root.lift()
    root.attributes("-topmost", True)
    root.after(200, lambda: root.attributes("-topmost", False))


def ask_setup(installed: str | None) -> str:
    """What to do with this executable: "install", "run", "open" or "cancel".

    - nothing installed: install, or run it as it is;
    - an older version installed: update it;
    - the same one: open it, or reinstall it;
    - a newer one: open the installed copy.
    """
    from ..installer import version_tuple

    title = product.APP_NAME
    if installed is None:
        heading = t("Install {app} {version}", app=product.APP_NAME, version=__version__)
        text = t("It is installed for every account on this PC, since it drives "
                 "screens they all share. Windows will ask for an administrator.")
        buttons = [("install", t("Install")), ("run", t("Run without installing"))]
    elif version_tuple(installed) < version_tuple(__version__):
        heading = t("Update {app} {old} to {new}", app=product.APP_NAME,
                    old=installed, new=__version__)
        text = t("The running application closes during the update, in every "
                 "session, and starts again afterwards. Your configuration and "
                 "history are kept. Windows will ask for an administrator.")
        buttons = [("install", t("Update")), ("run", t("Run without installing"))]
    elif version_tuple(installed) == version_tuple(__version__):
        heading = t("{app} {version} is already installed",
                    app=product.APP_NAME, version=installed)
        # Reinstalling the same version repairs a damaged installation.
        text = t("Open the installed copy, or reinstall it to repair it.")
        buttons = [("open", t("Open")), ("install", t("Reinstall"))]
    else:
        heading = t("{app} {version} is already installed",
                    app=product.APP_NAME, version=installed)
        text = t("This file is version {version}. Open the installed copy instead?",
                 version=__version__)
        buttons = [("open", t("Open")), ("run", t("Run this file anyway"))]
    buttons.append(("cancel", t("Cancel")))

    root, frame = _window(title)
    choice = {"value": "cancel"}
    body = ttk.Frame(frame)
    body.pack(side="left", fill="both", expand=True)
    ttk.Label(body, text=heading, style="Title.TLabel").pack(anchor="w")
    ttk.Label(body, text=text, wraplength=420, justify="left").pack(anchor="w", pady=(8, 16))
    row = ttk.Frame(body)
    row.pack(anchor="e")

    def pick(value: str) -> None:
        choice["value"] = value
        root.destroy()

    for index, (value, label) in enumerate(buttons):
        button = ttk.Button(row, text=label, command=lambda v=value: pick(v))
        button.pack(side="left", padx=(0 if index == 0 else 8, 0))
        if index == 0:
            button.focus_set()
    root.bind("<Return>", lambda _e: pick(buttons[0][0]))
    root.bind("<Escape>", lambda _e: pick("cancel"))
    root.protocol("WM_DELETE_WINDOW", lambda: pick("cancel"))
    _center(root)
    root.mainloop()
    return choice["value"]


def confirm_uninstall() -> tuple[bool, bool]:
    """Ask before removing: (go ahead, also delete the data)."""
    root, frame = _window(product.APP_NAME)
    result = {"go": False}
    body = ttk.Frame(frame)
    body.pack(side="left", fill="both", expand=True)
    ttk.Label(body, text=t("Uninstall {app}?", app=product.APP_NAME),
              style="Title.TLabel").pack(anchor="w")
    ttk.Label(
        body,
        text=t("The application stops in every session and is removed for every "
               "account."),
        wraplength=420, justify="left",
    ).pack(anchor="w", pady=(8, 8))
    keep = tk.BooleanVar(root, value=True)
    ttk.Checkbutton(
        body,
        text=t("Keep the configuration and the consumption history"),
        variable=keep,
    ).pack(anchor="w", pady=(0, 16))
    row = ttk.Frame(body)
    row.pack(anchor="e")

    def go() -> None:
        result["go"] = True
        root.destroy()

    ttk.Button(row, text=t("Uninstall"), command=go).pack(side="left")
    ttk.Button(row, text=t("Cancel"), command=root.destroy).pack(side="left", padx=(8, 0))
    root.bind("<Escape>", lambda _e: root.destroy())
    _center(root)
    root.mainloop()
    return result["go"], not keep.get()


def show_message(text: str, error: bool = False) -> None:
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    icon_module.apply_to_window(root)
    (messagebox.showerror if error else messagebox.showinfo)(product.APP_NAME, text, parent=root)
    root.destroy()
