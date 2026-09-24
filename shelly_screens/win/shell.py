"""Icone de notification, menu contextuel et evenements systeme.

Une unique fenetre Win32, creee mais jamais affichee, sert de point d'ancrage
a tout ce qui vient du systeme :

* l'icone de la zone de notification et son menu ;
* WM_POWERBROADCAST, pour couper les ecrans a la mise en veille et les
  remettre au reveil ;
* WM_QUERYENDSESSION / WM_ENDSESSION, meme chose a l'arret et au redemarrage ;
* WM_DISPLAYCHANGE, pour suivre les ecrans qui apparaissent et disparaissent ;
* le message "TaskbarCreated", qui signale un redemarrage de l'explorateur et
  impose de reposer l'icone.

La fenetre doit etre de premier niveau, et non message-only : Windows
n'adresse pas les diffusions d'alimentation et d'affichage a ces dernieres.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from . import hotkey as hotkey_module
from .api import HICON, LRESULT, UINT_PTR, kernel32, shell32, user32

# --------------------------------------------------------------- constantes

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_QUIT = 0x0012
WM_TIMER = 0x0113
WM_DISPLAYCHANGE = 0x007E
WM_POWERBROADCAST = 0x0218
WM_USER = 0x0400
WM_TRAY_CALLBACK = WM_USER + 1
# Envoye par une seconde instance : elle ne demarre pas et demande a
# celle-ci de se montrer, plutot que d'afficher un refus.
WM_SHOW_SETTINGS = WM_USER + 3
# Pose ou retire le raccourci global. Envoye par `set_hotkey`, depuis
# n'importe quel thread : Windows lie un raccourci au thread qui le pose.
WM_SET_HOTKEY = WM_USER + 4
WM_HOTKEY = 0x0312

WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_INFO = 0x10

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

MF_STRING = 0x0000
MF_POPUP = 0x0010
MF_SEPARATOR = 0x0800
MF_CHECKED = 0x0008
MF_DISABLED = 0x0002
MF_GRAYED = 0x0001

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

CW_USEDEFAULT = -2147483648
WS_OVERLAPPED = 0x00000000


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", HICON),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE,
    wintypes.LPCWSTR,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.DestroyIcon.argtypes = [HICON]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, UINT_PTR, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, UINT_PTR, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = UINT_PTR
user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.SendMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.SendMessageW.restype = LRESULT
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


@dataclass
class MenuItem:
    """Une entree du menu contextuel.

    Une entree porte soit une action, soit un sous-menu, soit rien -- une
    entree sans action ni sous-menu sert de ligne d'information et s'affiche
    grisee.
    """

    label: str
    action: Callable[[], None] | None = None
    checked: bool = False
    enabled: bool = True
    separator: bool = False
    submenu: list["MenuItem"] | None = None

    @staticmethod
    def sep() -> "MenuItem":
        return MenuItem(label="", separator=True)

    @staticmethod
    def info(label: str) -> "MenuItem":
        return MenuItem(label=label, enabled=False)


class TrayWindow:
    """Fenetre cachee portant l'icone de notification et les evenements systeme."""

    CLASS_NAME = "ShellyScreensTrayWindow"
    WINDOW_TITLE = "Shelly Screens"
    TIMER_ID = 1
    HOTKEY_ID = 1

    def __init__(
        self,
        tooltip: str = "Shelly Screens",
        on_suspend: Callable[[], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        on_shutdown: Callable[[], None] | None = None,
        on_display_change: Callable[[], None] | None = None,
        on_tick: Callable[[], None] | None = None,
        on_activate: Callable[[], None] | None = None,
        on_hotkey: Callable[[], None] | None = None,
        build_menu: Callable[[], list[MenuItem]] | None = None,
        tick_interval_ms: int = 5000,
    ) -> None:
        self.tooltip = tooltip
        self.on_suspend = on_suspend
        self.on_resume = on_resume
        self.on_shutdown = on_shutdown
        self.on_display_change = on_display_change
        self.on_tick = on_tick
        self.on_activate = on_activate
        self.on_hotkey = on_hotkey
        self.build_menu = build_menu or (lambda: [])
        self.tick_interval_ms = tick_interval_ms

        self._hwnd: int | None = None
        self._hicon: int | None = None
        self._icon_added = False
        self._menu_actions: dict[int, Callable[[], None]] = {}
        self._taskbar_created_message = 0
        self._thread_id = 0
        # Raccourci effectivement tenu, et celui dont la pose est en cours :
        # le message ne transporte que des codes, pas l'objet.
        self.hotkey: hotkey_module.Hotkey | None = None
        self._requested: hotkey_module.Hotkey | None = None
        self._hotkey_lock = threading.Lock()
        # La reference doit survivre a la fonction : Windows garde le pointeur.
        self._wndproc = WNDPROC(self._window_proc)

    # ------------------------------------------------------------- cycle de vie

    def create(self) -> None:
        """Enregistre la classe et cree la fenetre. A appeler dans son thread."""
        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSEXW()
        window_class.cbSize = ctypes.sizeof(WNDCLASSEXW)
        window_class.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        window_class.hInstance = instance
        window_class.lpszClassName = self.CLASS_NAME
        # Une classe deja enregistree renvoie 0 : sans importance ici.
        user32.RegisterClassExW(ctypes.byref(window_class))

        self._hwnd = user32.CreateWindowExW(
            0,
            self.CLASS_NAME,
            self.WINDOW_TITLE,
            WS_OVERLAPPED,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            0,
            0,
            None,
            None,
            instance,
            None,
        )
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        self._thread_id = kernel32.GetCurrentThreadId()
        # Si l'explorateur redemarre, la zone de notification est recreee vide.
        self._taskbar_created_message = user32.RegisterWindowMessageW("TaskbarCreated")
        if self.tick_interval_ms > 0:
            user32.SetTimer(self._hwnd, self.TIMER_ID, self.tick_interval_ms, None)

    def run(self) -> None:
        """Boucle de messages. Rend la main quand la fenetre est detruite."""
        if self._hwnd is None:
            self.create()
        message = wintypes.MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result in (0, -1):  # WM_QUIT ou erreur
                break
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def stop(self) -> None:
        """Demande l'arret de la boucle depuis n'importe quel thread."""
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    # --------------------------------------------------------------- raccourci

    def set_hotkey(self, wanted: "hotkey_module.Hotkey | None") -> bool:
        """Pose le raccourci global, ou le retire avec `None`.

        Rend faux si Windows le refuse -- un autre programme le tient. Le
        precedent est alors conserve : mieux vaut l'ancien raccourci que
        plus de raccourci du tout.
        """
        if self._hwnd is None:
            return False
        with self._hotkey_lock:
            self._requested = wanted
            return bool(user32.SendMessageW(
                self._hwnd,
                WM_SET_HOTKEY,
                wanted.modifiers if wanted else 0,
                wanted.vk if wanted else 0,
            ))

    def _register_hotkey(self, hwnd, modifiers: int, vk: int) -> int:
        previous = self.hotkey
        user32.UnregisterHotKey(hwnd, self.HOTKEY_ID)
        self.hotkey = None
        if not vk:
            return 1
        flags = modifiers | hotkey_module.MOD_NOREPEAT
        if user32.RegisterHotKey(hwnd, self.HOTKEY_ID, flags, vk):
            self.hotkey = self._requested
            return 1
        if previous is not None and user32.RegisterHotKey(
            hwnd, self.HOTKEY_ID, previous.modifiers | hotkey_module.MOD_NOREPEAT,
            previous.vk,
        ):
            self.hotkey = previous
        return 0

    # ------------------------------------------------------------------ icone

    def set_icon(self, ico_path: str, tooltip: str | None = None) -> None:
        """Pose ou met a jour l'icone de la zone de notification."""
        if self._hwnd is None:
            return
        if tooltip is not None:
            self.tooltip = tooltip

        previous = self._hicon
        handle = user32.LoadImageW(None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        if not handle:
            return
        self._hicon = handle

        data = self._icon_data()
        data.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
        data.uCallbackMessage = WM_TRAY_CALLBACK
        data.hIcon = handle
        data.szTip = self.tooltip[:127]
        action = NIM_MODIFY if self._icon_added else NIM_ADD
        if not shell32.Shell_NotifyIconW(action, ctypes.byref(data)):
            # Un NIM_MODIFY echoue si l'icone a disparu : on la repose.
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
        self._icon_added = True

        if previous:
            user32.DestroyIcon(previous)

    def notify(self, title: str, message: str) -> None:
        """Affiche une bulle d'information."""
        if self._hwnd is None or not self._icon_added:
            return
        data = self._icon_data()
        data.uFlags = NIF_INFO
        data.szInfoTitle = title[:63]
        data.szInfo = message[:255]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def remove_icon(self) -> None:
        if self._hwnd is None or not self._icon_added:
            return
        data = self._icon_data()
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        self._icon_added = False
        if self._hicon:
            user32.DestroyIcon(self._hicon)
            self._hicon = None

    def _icon_data(self) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        return data

    # ------------------------------------------------------------------- menu

    def show_menu(self) -> None:
        """Affiche le menu contextuel a l'emplacement du curseur."""
        items = self.build_menu()
        if not items:
            return
        self._menu_actions = {}
        self._next_command_id = 1000
        menu = self._build_popup(items)

        from .api import POINT  # local : seul cet endroit en a besoin

        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # Sans cet appel, le menu ne se referme pas quand on clique ailleurs.
        user32.SetForegroundWindow(self._hwnd)
        chosen = user32.TrackPopupMenu(
            menu,
            TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
            point.x,
            point.y,
            0,
            self._hwnd,
            None,
        )
        user32.PostMessageW(self._hwnd, 0, 0, 0)  # contournement connu
        user32.DestroyMenu(menu)  # detruit aussi les sous-menus rattaches
        action = self._menu_actions.get(int(chosen))
        if action is not None:
            action()

    def _build_popup(self, items: list[MenuItem]) -> int:
        """Construit un menu et ses sous-menus, en numerotant les commandes."""
        menu = user32.CreatePopupMenu()
        for item in items:
            if item.separator:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                continue

            flags = MF_STRING
            if item.checked:
                flags |= MF_CHECKED
            # Une entree sans action ni sous-menu n'est qu'une ligne d'information.
            if not item.enabled or (item.action is None and not item.submenu):
                flags |= MF_DISABLED | MF_GRAYED

            if item.submenu:
                submenu = self._build_popup(item.submenu)
                user32.AppendMenuW(menu, flags | MF_POPUP, submenu, item.label)
                continue

            command_id = self._next_command_id
            self._next_command_id += 1
            user32.AppendMenuW(menu, flags, command_id, item.label)
            if item.action is not None:
                self._menu_actions[command_id] = item.action
        return menu

    # --------------------------------------------------- traitement des messages

    def _window_proc(self, hwnd, message, wparam, lparam) -> int:
        try:
            handled = self._dispatch(hwnd, message, wparam, lparam)
        except Exception:  # noqa: BLE001 - une exception ici tuerait la boucle
            # Sans console, un traceback imprime se perdrait : il part au journal.
            import logging

            logging.getLogger("shelly_screens").exception(
                "Error while handling window message %s", message
            )
            handled = None
        if handled is not None:
            return handled
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _dispatch(self, hwnd, message, wparam, lparam) -> int | None:
        if message == WM_TRAY_CALLBACK:
            event = lparam & 0xFFFF
            if event == WM_RBUTTONUP:
                self.show_menu()
            elif event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                if self.on_activate is not None:
                    self.on_activate()
                else:
                    self.show_menu()
            return 0

        if message == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                # Windows attend ici : la coupure doit etre breve.
                if self.on_suspend is not None:
                    self.on_suspend()
            elif wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                if self.on_resume is not None:
                    self.on_resume()
            return 1  # TRUE : l'evenement est pris en compte

        if message == WM_QUERYENDSESSION:
            # Arret ou redemarrage : meme traitement que la mise en veille,
            # car le PC va repartir sans que rien ne pilote la multiprise.
            if self.on_shutdown is not None:
                self.on_shutdown()
            return 1  # ne pas s'opposer a la fermeture de session

        if message == WM_ENDSESSION:
            if wparam:
                self.remove_icon()
            return 0

        if message == WM_DISPLAYCHANGE:
            if self.on_display_change is not None:
                self.on_display_change()
            return 0

        if message == WM_TIMER and wparam == self.TIMER_ID:
            if self.on_tick is not None:
                self.on_tick()
            return 0

        if message == WM_SHOW_SETTINGS:
            if self.on_activate is not None:
                self.on_activate()
            return 0

        if message == WM_SET_HOTKEY:
            return self._register_hotkey(hwnd, wparam, lparam)

        if message == WM_HOTKEY and wparam == self.HOTKEY_ID:
            if self.on_hotkey is not None:
                self.on_hotkey()
            return 0

        if self._taskbar_created_message and message == self._taskbar_created_message:
            # L'explorateur a redemarre : l'icone doit etre reposee.
            self._icon_added = False
            if self._hicon:
                data = self._icon_data()
                data.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
                data.uCallbackMessage = WM_TRAY_CALLBACK
                data.hIcon = self._hicon
                data.szTip = self.tooltip[:127]
                shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
                self._icon_added = True
            return 0

        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0

        if message == WM_DESTROY:
            user32.UnregisterHotKey(hwnd, self.HOTKEY_ID)
            self.remove_icon()
            user32.PostQuitMessage(0)
            return 0

        return None
