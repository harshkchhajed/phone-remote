"""Fake mouse and keyboard input on the X11 display through libXtst (ctypes, no extra packages)."""
import ctypes
import time
from ctypes import POINTER, byref, c_char_p, c_int, c_long, c_uint, c_ulong, c_void_p

_x = ctypes.cdll.LoadLibrary("libX11.so.6")
_t = ctypes.cdll.LoadLibrary("libXtst.so.6")

_x.XOpenDisplay.restype = c_void_p
_x.XOpenDisplay.argtypes = [c_char_p]
_x.XFlush.argtypes = [c_void_p]
_x.XSync.argtypes = [c_void_p, c_int]
_x.XStringToKeysym.restype = c_ulong
_x.XStringToKeysym.argtypes = [c_char_p]
_x.XKeysymToKeycode.restype = ctypes.c_ubyte
_x.XKeysymToKeycode.argtypes = [c_void_p, c_ulong]
_x.XkbKeycodeToKeysym.restype = c_ulong
_x.XkbKeycodeToKeysym.argtypes = [c_void_p, ctypes.c_ubyte, c_int, c_int]
_x.XDisplayKeycodes.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int)]
_x.XGetKeyboardMapping.restype = POINTER(c_ulong)
_x.XGetKeyboardMapping.argtypes = [c_void_p, ctypes.c_ubyte, c_int, POINTER(c_int)]
_x.XChangeKeyboardMapping.argtypes = [c_void_p, c_int, c_int, POINTER(c_ulong), c_int]
_x.XFree.argtypes = [c_void_p]
_x.XInternAtom.restype = c_ulong
_x.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
_x.XDefaultRootWindow.restype = c_ulong
_x.XDefaultRootWindow.argtypes = [c_void_p]
_x.XGetWindowProperty.argtypes = [c_void_p, c_ulong, c_ulong, c_long, c_long, c_int, c_ulong, POINTER(c_ulong),
                                  POINTER(c_int), POINTER(c_ulong), POINTER(c_ulong), POINTER(POINTER(ctypes.c_ubyte))]
_x.XQueryKeymap.argtypes = [c_void_p, ctypes.c_char * 32]
_x.XSendEvent.argtypes = [c_void_p, c_ulong, c_int, c_long, c_void_p]
_t.XTestFakeKeyEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]
_t.XTestFakeButtonEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]
_t.XTestFakeRelativeMotionEvent.argtypes = [c_void_p, c_int, c_int, c_ulong]

# Friendly names the web UI sends, mapped to X keysym names.
ALIASES = {
    "ctrl": "Control_L", "alt": "Alt_L", "shift": "Shift_L", "super": "Super_L",
    "enter": "Return", "backspace": "BackSpace", "esc": "Escape", "tab": "Tab",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "Page_Up", "pagedown": "Page_Down",
    "delete": "Delete", "space": "space",
    "volup": "XF86AudioRaiseVolume", "voldown": "XF86AudioLowerVolume",
    "mute": "XF86AudioMute", "playpause": "XF86AudioPlay",
    "prev": "XF86AudioPrev", "next": "XF86AudioNext",
}


class _ClientMessage(ctypes.Structure):
    _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
                ("window", c_ulong), ("message_type", c_ulong), ("format", c_int), ("data", c_long * 5)]


class _XEvent(ctypes.Union):
    _fields_ = [("xclient", _ClientMessage), ("pad", c_long * 24)]


# A window can vanish between listing and reading it. Xlib's default error handler would exit the process.
_ignore_errors = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)(lambda d, e: 0)
_x.XSetErrorHandler(_ignore_errors)


class XInput:
    def __init__(self):
        self.dpy = _x.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("cannot open X display (is DISPLAY set?)")
        self.shift = _x.XKeysymToKeycode(self.dpy, _x.XStringToKeysym(b"Shift_L"))
        self.scratch = self._find_free_keycode()

    def _find_free_keycode(self):
        lo, hi, per = c_int(), c_int(), c_int()
        _x.XDisplayKeycodes(self.dpy, byref(lo), byref(hi))
        count = hi.value - lo.value + 1
        syms = _x.XGetKeyboardMapping(self.dpy, lo.value, count, byref(per))
        free = None
        for i in range(count - 1, -1, -1):
            if all(syms[i * per.value + j] == 0 for j in range(per.value)):
                free = lo.value + i
                break
        _x.XFree(syms)
        return free

    # --- windows (EWMH, as a taskbar sees them) ---
    def _atom(self, name):
        return _x.XInternAtom(self.dpy, name.encode(), 0)

    def _prop(self, w, name):
        """Return a property as a list of ints (format 32) or bytes (format 8), or None."""
        typ, fmt, n, after = c_ulong(), c_int(), c_ulong(), c_ulong()
        data = POINTER(ctypes.c_ubyte)()
        if _x.XGetWindowProperty(self.dpy, w, self._atom(name), 0, 1 << 16, 0, 0, byref(typ), byref(fmt),
                                 byref(n), byref(after), byref(data)) != 0 or not data:
            return None
        try:
            if fmt.value == 32:  # Xlib hands back 32-bit items as C longs
                return list(ctypes.cast(data, POINTER(c_ulong))[:n.value])
            return bytes(data[:n.value]) if fmt.value == 8 else None
        finally:
            _x.XFree(data)

    def windows(self):
        """Top-level app windows the taskbar would show: [{"id", "title", "wm_class": (instance, class)}]."""
        normal = self._atom("_NET_WM_WINDOW_TYPE_NORMAL")
        skip = self._atom("_NET_WM_STATE_SKIP_TASKBAR")
        out = []
        for w in self._prop(_x.XDefaultRootWindow(self.dpy), "_NET_CLIENT_LIST") or []:
            types = self._prop(w, "_NET_WM_WINDOW_TYPE")
            if types and normal not in types or skip in (self._prop(w, "_NET_WM_STATE") or []):
                continue
            title = self._prop(w, "_NET_WM_NAME") or self._prop(w, "WM_NAME") or b""
            cls = (self._prop(w, "WM_CLASS") or b"").decode(errors="replace").split("\0")
            out.append({"id": w, "title": title.decode(errors="replace"),
                        "wm_class": (cls[0], cls[1] if len(cls) > 1 else "")})
        return out

    def _wm_message(self, w, name, *data):
        ev = _XEvent()
        ev.xclient.type = 33  # ClientMessage
        ev.xclient.window = w
        ev.xclient.message_type = self._atom(name)
        ev.xclient.format = 32
        for i, v in enumerate(data):
            ev.xclient.data[i] = v
        mask = (1 << 19) | (1 << 20)  # SubstructureNotify | SubstructureRedirect: route to the window manager
        _x.XSendEvent(self.dpy, _x.XDefaultRootWindow(self.dpy), 0, mask, byref(ev))
        _x.XFlush(self.dpy)

    def activate(self, w):
        """Raise and focus a window (switching workspace and unminimizing). Source 2 = pager, which WMs trust."""
        self._wm_message(w, "_NET_ACTIVE_WINDOW", 2, 0, 0)

    def active_class(self):
        """WM_CLASS class name of the focused window, or ""."""
        act = self._prop(_x.XDefaultRootWindow(self.dpy), "_NET_ACTIVE_WINDOW")
        cls = (self._prop(act[0], "WM_CLASS") or b"") if act and act[0] else b""
        return cls.decode(errors="replace").split("\0")[1] if cls.count(b"\0") >= 2 else ""

    def keys_down(self, names):
        """True while any of these keys is physically held."""
        keys = (ctypes.c_char * 32)()
        _x.XQueryKeymap(self.dpy, keys)
        for n in names:
            kc = _x.XKeysymToKeycode(self.dpy, _x.XStringToKeysym(ALIASES.get(n, n).encode()))
            if kc and keys.raw[kc // 8] >> (kc % 8) & 1:
                return True
        return False

    def close(self, w):
        """Ask politely, like clicking the title-bar X: the app may still prompt to save."""
        self._wm_message(w, "_NET_CLOSE_WINDOW", 0, 2)

    # --- mouse ---
    def move(self, dx, dy):
        _t.XTestFakeRelativeMotionEvent(self.dpy, int(dx), int(dy), 0)
        _x.XFlush(self.dpy)

    def button(self, b, down):
        _t.XTestFakeButtonEvent(self.dpy, int(b), 1 if down else 0, 0)
        _x.XFlush(self.dpy)

    def click(self, b):
        self.button(b, True)
        self.button(b, False)

    def scroll(self, dx, dy):
        # X has no smooth scroll via XTest: one wheel click per unit.
        for _ in range(min(abs(int(dy)), 20)):
            self.click(4 if dy < 0 else 5)
        for _ in range(min(abs(int(dx)), 20)):
            self.click(6 if dx < 0 else 7)

    # --- keyboard ---
    def _key(self, kc, down):
        _t.XTestFakeKeyEvent(self.dpy, kc, 1 if down else 0, 0)

    def _keysym_for_char(self, ch):
        code = ord(ch)
        if ch == "\n":
            return _x.XStringToKeysym(b"Return")
        if ch == "\t":
            return _x.XStringToKeysym(b"Tab")
        return code if 0x20 <= code <= 0xFF else 0x01000000 + code

    def _tap_keysym(self, sym):
        kc = _x.XKeysymToKeycode(self.dpy, sym)
        if kc:
            needs_shift = _x.XkbKeycodeToKeysym(self.dpy, kc, 0, 0) != sym and \
                _x.XkbKeycodeToKeysym(self.dpy, kc, 0, 1) == sym
            if needs_shift:
                self._key(self.shift, True)
            self._key(kc, True)
            self._key(kc, False)
            if needs_shift:
                self._key(self.shift, False)
            _x.XFlush(self.dpy)
            return
        if self.scratch is None:
            return
        # Character not on the keyboard layout (emoji, accents): bind it to a spare keycode.
        pair = (c_ulong * 2)(sym, sym)
        _x.XChangeKeyboardMapping(self.dpy, self.scratch, 2, pair, 1)
        _x.XSync(self.dpy, 0)
        time.sleep(0.02)
        self._key(self.scratch, True)
        self._key(self.scratch, False)
        _x.XSync(self.dpy, 0)
        # Let the focused app read the key before the next remap changes its meaning.
        time.sleep(0.06)

    def type_text(self, text):
        for ch in text:
            self._tap_keysym(self._keysym_for_char(ch))

    def combo(self, names):
        """Press keys in order and release in reverse, e.g. ["ctrl", "c"]."""
        codes = []
        for name in names:
            sym = _x.XStringToKeysym(ALIASES.get(name, name).encode())
            kc = _x.XKeysymToKeycode(self.dpy, sym) if sym else 0
            if kc:
                codes.append(kc)
        for kc in codes:
            self._key(kc, True)
        for kc in reversed(codes):
            self._key(kc, False)
        _x.XFlush(self.dpy)
