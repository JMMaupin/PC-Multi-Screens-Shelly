"""Small profile picker window, opened by the global shortcut.

One button per profile; a click applies the profile, without confirmation,
and the window closes. It opens in the foreground, centered on the primary
screen -- the one Windows designates as such, where its own dialogs also
appear.

Designed for the keyboard as much as for the mouse: the arrows move the
selection, Enter applies, digits 1 to 9 directly apply the first nine
profiles, Esc closes, and so does pressing the shortcut a second time.
Mouse hover moves the same selection: a single highlight, never two that
contradict each other. It closes by itself when you click elsewhere:
a quick choice lingering on screen would become clutter.

Below the buttons, the screen map, drawn as in the settings. On opening,
it shows the actual state; as soon as the selection moves, it shows what
the selected profile would give. A click on a screen starts from what is
displayed and toggles its planned state, without switching anything: it
is Enter, or the Apply button, that carries out the selection. It is
always a one-off configuration, outside profiles: the clicks change no
profile, and no profile becomes "current". Changing the selection
discards the clicks: the map always shows what will happen on confirm.
"""

from __future__ import annotations

import ctypes
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from . import screen_map
from . import theme as theme_module
from ..i18n import t
from ..win import icon as icon_module
from ..win import monitors

if TYPE_CHECKING:
    from ..app import Application

_state_lock = threading.Lock()
_is_open = False
_current: "ProfilePicker | None" = None

# Button width, in characters: enough for an ordinary profile name,
# without the window sprawling.
BUTTON_WIDTH = 26
MAP_HEIGHT = 120  # the screen map, below the buttons


def toggle_picker(application: "Application") -> None:
    """Open the window, or close it if it is already open.

    Called from the Windows message thread: must not block anything.
    """
    global _is_open
    with _state_lock:
        if _is_open:
            picker = _current
            if picker is not None:
                try:
                    picker.root.after(0, picker.close)
                except Exception:  # noqa: BLE001 - window already closing
                    pass
            return
        _is_open = True

    def run() -> None:
        global _is_open, _current
        try:
            root = tk.Tk()
            _current = ProfilePicker(root, application)
            root.mainloop()
        except Exception as exc:  # noqa: BLE001 - a failed UI must not kill the app
            application.log(f"Profile picker failed: {exc}")
        finally:
            with _state_lock:
                _is_open = False
                _current = None

    threading.Thread(target=run, name="picker-ui", daemon=True).start()


class ProfilePicker:
    """The profile buttons, and nothing else."""

    def __init__(self, root: tk.Tk, application: "Application") -> None:
        self.root = root
        self.app = application
        self.closing = False
        config = application.config

        root.withdraw()  # built off-screen, shown once positioned
        root.title(t("Profiles"))
        icon_module.apply_to_window(root)
        root.resizable(False, False)
        # Tool window: no taskbar button for a box that only lives a few
        # seconds.
        root.attributes("-toolwindow", True)
        palette = theme_module.apply(root, config.settings.theme)
        self.palette = palette
        root.configure(background=palette.bg)
        theme_module.apply_titlebar(root, palette.dark)

        style = ttk.Style(root)
        # The selection takes the accent, as in a menu: it is what the eye
        # follows when navigating with the arrows. Focus takes precedence
        # over hover, so the chosen button stays marked under the mouse.
        hover = theme_module._mix(palette.surface_alt, palette.accent, 0.25)
        style.map(
            "Picker.TButton",
            background=[("focus", palette.accent), ("active", hover)],
            foreground=[("focus", palette.accent_text)],
            lightcolor=[("focus", palette.accent)],
            darkcolor=[("focus", palette.accent)],
        )

        body = ttk.Frame(root, padding=14)
        body.pack(fill="both", expand=True)
        profiles = config.sorted_profiles()
        self.profiles = profiles
        current = config.settings.last_profile
        self.buttons: list[ttk.Button] = []
        # The current profile: the initial selection, and a check mark. It
        # stays clickable -- reapplying it is useful after a screen was
        # turned back on by hand.
        self.start = 0
        if not profiles:
            ttk.Label(body, text=t("No profile configured")).pack(padx=20, pady=10)
        for index, profile in enumerate(profiles):
            label = f"{index + 1}   {profile.label}" if index < 9 else f"     {profile.label}"
            if profile.name == current:
                label += "   ✓"
                self.start = index
            button = ttk.Button(
                body,
                text=label,
                width=BUTTON_WIDTH,
                style="Picker.TButton",
                command=lambda name=profile.name: self.choose(name),
            )
            button.pack(fill="x", pady=3)
            button.bind("<Enter>", lambda _e, i=index: self._hover(i))
            self.buttons.append(button)
            if index < 9:
                root.bind(str(index + 1), lambda _e, name=profile.name: self.choose(name))
        self._build_map(body)
        self.hint = tk.StringVar(root, value=t("Arrows and Enter to choose, Esc to close"))
        ttk.Label(
            body, textvariable=self.hint, style="Hint.TLabel", anchor="center",
        ).pack(fill="x", pady=(8, 0))

        root.bind("<Escape>", lambda _e: self.close())
        for key, step in (("<Up>", -1), ("<Left>", -1), ("<Down>", 1), ("<Right>", 1)):
            root.bind(key, lambda _e, step=step: self._move(step))
        root.bind("<Home>", lambda _e: self._select(0))
        root.bind("<End>", lambda _e: self._select(len(self.buttons) - 1))
        root.bind("<Return>", self._invoke)
        root.bind("<KP_Enter>", self._invoke)
        root.bind("<FocusOut>", self._focus_out)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._show()

    # ------------------------------------------------------------- display

    def _show(self) -> None:
        """Center on the primary screen, then bring to the foreground."""
        root = self.root
        root.update_idletasks()
        width, height = root.winfo_reqwidth(), root.winfo_reqheight()
        left, top, right, bottom = _primary_work_area(root)
        x = left + (right - left - width) // 2
        y = top + (bottom - top - height) // 2
        root.geometry(f"+{x}+{y}")
        root.deiconify()
        root.attributes("-topmost", True)
        root.lift()
        root.update_idletasks()
        # Tk alone is not enough: Windows refuses the foreground to whoever
        # did not ask for it. Pressing the shortcut grants us that right,
        # but it still has to be claimed for the right window -- the frame
        # Windows knows, not Tk's inner widget.
        try:
            ctypes.windll.user32.SetForegroundWindow(int(root.wm_frame(), 16))
        except (ValueError, OSError):
            pass
        root.focus_force()
        # On opening, the map keeps the actual state: the preview only
        # starts on the first move of the selection.
        self._select(self.start, preview=False)

    # ------------------------------------------------------------- keyboard

    def _select(self, index: int, preview: bool = True) -> str:
        if self.buttons:
            index %= len(self.buttons)
            self.buttons[index].focus_set()
            if preview:
                self._preview(self.profiles[index])
        return "break"

    def _hover(self, index: int) -> None:
        """Hover moves the selection -- without discarding the clicks if nothing
        changes: hovering again over the already chosen button means nothing."""
        if self.root.focus_get() is not self.buttons[index]:
            self._select(index)

    def _move(self, step: int) -> str:
        """Next or previous selection, wrapping around at the ends."""
        focused = self.root.focus_get()
        index = self.buttons.index(focused) if focused in self.buttons else self.start - step
        return self._select(index + step)

    def _invoke(self, _event) -> str:
        # Screens were toggled on the map: Enter confirms that choice.
        if self._changed():
            self.apply_selection()
            return "break"
        focused = self.root.focus_get()
        if focused in self.buttons:
            focused.invoke()
        return "break"

    def _focus_out(self, _event) -> None:
        # Focus moves from one button to another without leaving the window;
        # only close if it has left the application.
        self.root.after(150, self._close_if_inactive)

    def _close_if_inactive(self) -> None:
        if self.closing:
            return
        try:
            if self.root.focus_get() is None:
                self.close()
        except (tk.TclError, KeyError):
            # `focus_get` fails when the focus is on another program's
            # window: this window is no longer active.
            self.close()

    # --------------------------------------------------------------- map

    def _build_map(self, body: ttk.Frame) -> None:
        """The screen map and its Apply button, if there is a layout."""
        config = self.app.config
        # The actual state of the screen outlets: the starting point for clicks.
        self.actual = {
            o.ref: bool(self.app.states[o.ref].output)
            for o in config.outlets
            if o.monitor_key and o.ref in self.app.states
        }
        self.pending = dict(self.actual)
        # True once screens have been toggled by hand: Enter then confirms
        # these clicks, not the selected profile.
        self.edited = False
        self.map = None
        self.apply_button = None
        if not config.screens:
            return
        self.map = screen_map.ScreenMap(
            body, palette=lambda: self.palette, on_toggle=self._toggle,
            empty_text="", compact=True, height=MAP_HEIGHT,
        )
        self.map.canvas.pack(fill="x", pady=(10, 4))
        self.apply_button = ttk.Button(
            body, text=t("Apply"), command=self.apply_selection, state="disabled"
        )
        self.apply_button.pack(fill="x")
        self._draw_map()

    def _draw_map(self) -> None:
        if self.map is None:
            return
        self.map.show(screen_map.build_tiles(
            self.app.config, lambda ref: self.pending.get(ref), editable=True,
        ))

    def _preview(self, profile) -> None:
        """Show what this profile would give; discards pending clicks."""
        if self.map is None:
            return
        config = self.app.config
        self.pending = {
            ref: profile.wants(ref) or config.outlet(ref).never_switch_off
            for ref in self.actual
        }
        self.edited = False
        self._refresh_controls()

    def _toggle(self, ref: str) -> None:
        """Toggle a screen's planned state: nothing is switched yet."""
        if ref not in self.pending:
            return  # silent device: we don't know what we would change
        self.pending[ref] = not self.pending[ref]
        self.edited = True
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        changed = self._changed()
        allowed = self._keeps_a_screen()
        self.apply_button.configure(state="normal" if changed and allowed else "disabled")
        if changed and not allowed:
            hint = t("At least one screen must stay on")
        elif changed:
            hint = t("Enter or Apply to switch the screens, Esc to cancel")
        else:
            hint = t("Arrows and Enter to choose, Esc to close")
        self.hint.set(hint)
        self._draw_map()

    def _changed(self) -> bool:
        """Clicks to confirm, that would actually change something."""
        return self.edited and self.pending != self.actual

    def _keeps_a_screen(self) -> bool:
        """True if the map selection leaves at least one screen on."""
        powered = {ref for ref, state in self.app.states.items() if state.output}
        powered -= set(self.pending)
        powered |= {ref for ref, on in self.pending.items() if on}
        return self.app.config.leaves_a_screen(powered)

    def apply_selection(self) -> None:
        """Carry out the screens toggled on the map, then close.

        A one-off configuration, even if it looks like a profile: choosing
        on the map means wanting something other than the profiles. Only
        the screens that change are switched; the rest stays as is.
        """
        if self.closing or not self._changed() or not self._keeps_a_screen():
            return
        config = self.app.config
        changes = {ref: on for ref, on in self.pending.items() if on != self.actual[ref]}
        self.app.log(
            "Screen selection from the keyboard shortcut: "
            + ", ".join(f"{config.outlet(r).label} {'on' if on else 'off'}"
                        for r, on in changes.items())
        )
        self.app.apply_outlets(changes)
        self.close()

    # --------------------------------------------------------------- actions

    def choose(self, name: str) -> None:
        """Apply the profile and close, without asking anything."""
        if self.closing:
            return
        self.app.log(f"Profile '{name}' chosen from the keyboard shortcut")
        # The application handles the network in its own thread: the
        # window can close without waiting for the devices.
        self.app.apply_profile(name)
        self.close()

    def close(self) -> None:
        self.closing = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def _primary_work_area(root: tk.Misc) -> tuple[int, int, int, int]:
    """Work area of the primary screen, taskbar excluded."""
    for monitor in monitors.list_monitors():
        if monitor.is_primary:
            return monitor.work_rect
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
