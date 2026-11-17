#!/usr/bin/python3
"""Read or set the X clipboard through GTK (no xclip on this machine).

  clip.py get clipboard|primary IMAGE_OUT   prints JSON: {"kind": "files", "paths": [...]} | {"kind": "text", "text": ...}
                                            | {"kind": "image", "path": IMAGE_OUT} | {"kind": "empty"}
  clip.py set-text                          clipboard = stdin (UTF-8)
  clip.py set-image FILE                    clipboard = that picture

On X11 the clipboard lives in the process that set it, so "set-*" keeps running until something else takes the clipboard.
"""
import json
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib, Gdk, Gtk  # noqa: E402


def get(which, image_out):
    cb = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY if which == "primary" else Gdk.SELECTION_CLIPBOARD)
    # Files first (a file manager also offers their paths as text), then text, then a picture.
    uris = cb.wait_for_uris() or []
    paths = [GLib.filename_from_uri(u)[0] for u in uris if u.startswith("file://")]
    if paths:
        return {"kind": "files", "paths": paths}
    text = cb.wait_for_text()
    if text:
        return {"kind": "text", "text": text}
    img = cb.wait_for_image()
    if img:
        img.savev(image_out, "png", [], [])
        return {"kind": "image", "path": image_out}
    return {"kind": "empty"}


def hold(cb):
    """Serve the clipboard until another app takes it over."""
    cb.store()  # also hand a copy to the clipboard manager, if one runs
    GLib.timeout_add(1000, lambda: cb.connect("owner-change", lambda *a: Gtk.main_quit()) and False)
    Gtk.main()


def main():
    cmd = sys.argv[1]
    if cmd == "get":
        print(json.dumps(get(sys.argv[2], sys.argv[3])))
        return
    cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    if cmd == "set-text":
        cb.set_text(sys.stdin.buffer.read().decode("utf-8", "replace"), -1)
    elif cmd == "set-image":
        cb.set_image(GdkPixbuf.Pixbuf.new_from_file(sys.argv[2]))
    hold(cb)


if __name__ == "__main__":
    main()
