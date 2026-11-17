#!/usr/bin/python3
"""Phone Remote: control this laptop's mouse, keyboard and apps from a phone browser on the same network."""
import argparse
import asyncio
import configparser
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import time
from pathlib import Path

from aiohttp import WSMsgType, web

HERE = Path(__file__).resolve().parent
CONF = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "phone-remote"
DEVICES = CONF / "devices.json"
SLOTS = CONF / "slots.json"
TLS = CONF / "tls"
APP_DIRS = [
    Path.home() / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    Path.home() / ".local/share/flatpak/exports/share/applications",
    Path("/var/lib/snapd/desktop/applications"),
    Path("/usr/share/applications"),
]
DEFAULT_SLOTS = ["brave-browser.desktop", "com.anthropic.Claude.desktop",
                 "com.stremio.Stremio.desktop", "com.microsoft.VSCode.desktop"]
PAIR_TTL = 120
CLOSE_WAIT = 30  # seconds apps get to close before shutdown gives up and asks the phone
CLIP = HERE / "clip.py"
OUTBOX_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "phone-remote"  # laptop -> phone pictures
# Terminals copy with Ctrl+Shift+C (Ctrl+C would interrupt the running program). xterm only has the selection.
TERMINALS = {"gnome-terminal", "terminator", "tilix", "kitty", "alacritty", "konsole", "xfce4-terminal"}
XTERMS = {"xterm", "uxterm"}
PAIR_TRIES = 5

pairing = None  # {"id", "code", "name", "expires", "tries"}
_xin = None
clients = set()  # open phone WebSockets
outbox = None  # last thing sent with Alt+C: {"id", "kind", "text" or "files": [{"name", "size"}]}
outbox_paths = []  # files behind outbox["files"]


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(path, data):
    CONF.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def authorized(token):
    if not token:
        return None
    h = digest(token)
    for dev in load(DEVICES, []):
        if secrets.compare_digest(dev["hash"], h):
            return dev
    return None


def ensure_tls():
    """Create a local CA (trusted once on the phone) and a server certificate signed by it.

    Phone browsers now force HTTPS, so plain HTTP no longer works. The CA is name-constrained to
    .local/.lan/localhost/private IPs, so even if its key leaked it could not impersonate public sites.
    """
    host = os.uname().nodename
    TLS.mkdir(parents=True, exist_ok=True)
    os.chmod(TLS, 0o700)
    ca_key, ca_crt = TLS / "ca.key", TLS / "ca.crt"
    key, crt = TLS / "server.key", TLS / "server.crt"
    run = lambda *a: subprocess.run(["openssl", *a], check=True, capture_output=True)
    if not ca_crt.exists():
        run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
            "-keyout", str(ca_key), "-out", str(ca_crt), "-days", "3650",
            "-subj", f"/CN=Phone Remote CA ({host})",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-addext", "nameConstraints=critical,permitted;DNS:local,permitted;DNS:lan,permitted;DNS:localhost,"
                       "permitted;IP:192.168.0.0/255.255.0.0,permitted;IP:10.0.0.0/255.0.0.0,"
                       "permitted;IP:127.0.0.0/255.0.0.0")
        crt.unlink(missing_ok=True)
    expiring = not crt.exists() or subprocess.run(
        ["openssl", "x509", "-checkend", str(30 * 86400), "-noout", "-in", str(crt)], capture_output=True).returncode
    if expiring:
        ext = TLS / "server.ext"
        ext.write_text(f"subjectAltName=DNS:{host}-remote.local,DNS:{host}.lan,DNS:{host}.local,DNS:localhost,IP:127.0.0.1\n"
                       "basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n")
        csr = TLS / "server.csr"
        run("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
            "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={host}-remote.local")
        # iOS rejects server certificates valid for more than 825 days.
        run("x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
            "-out", str(crt), "-days", "800", "-extfile", str(ext))
    for f in TLS.iterdir():
        os.chmod(f, 0o600)
    return crt, key


def xin():
    global _xin
    if _xin is None:
        from xinput import XInput
        _xin = XInput()
    return _xin


# --- installed apps ---------------------------------------------------------

def scan_apps():
    apps = {}
    for d in APP_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.desktop")):
            if f.name in apps:
                continue
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            cp.optionxform = str
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            if e.get("Type") != "Application" or e.get("NoDisplay") == "true" or e.get("Hidden") == "true":
                continue
            apps[f.name] = {"id": f.name, "name": e.get("Name", f.stem), "icon": e.get("Icon", ""), "path": str(f),
                           "wmclass": e.get("StartupWMClass", "")}
    return apps


def get_slots(apps):
    slots = load(SLOTS, None)
    if not slots:
        present = [a for a in DEFAULT_SLOTS if a in apps]
        extra = [a for a in apps if a not in present]
        slots = (present + extra)[:4]
    return slots


def app_for_window(apps, wm_class):
    """Match a window's WM_CLASS to its .desktop entry, the way GNOME's dock does (roughly)."""
    names = {n.lower() for n in wm_class if n}
    for a in apps.values():
        stem = a["id"][:-len(".desktop")].lower()
        if a["wmclass"].lower() in names or stem in names or stem.rsplit(".", 1)[-1] in names:
            return a
    return None


def open_windows():
    apps = scan_apps()
    out = []
    for w in xin().windows():
        app = app_for_window(apps, w["wm_class"])
        out.append({"id": w["id"], "title": w["title"], "app": app["id"] if app else None,
                    "name": app["name"] if app else (w["wm_class"][1] or w["wm_class"][0] or "Window")})
    return out


def poweroff():
    subprocess.Popen(["systemctl", "poweroff"], start_new_session=True)


def icon_file(icon):
    if not icon:
        return None
    if os.path.isabs(icon):
        return icon if os.path.exists(icon) else None
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        info = Gtk.IconTheme.get_default().lookup_icon(icon, 128, 0)
        return info.get_filename() if info else None
    except Exception:
        return None


# --- HTTP handlers ----------------------------------------------------------

def bearer(request):
    h = request.headers.get("Authorization", "")
    return h[7:] if h.startswith("Bearer ") else ""


async def index(request):
    return web.FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


async def ca_cert(request):
    # This MIME type makes iOS offer to install it as a profile.
    return web.Response(body=(TLS / "ca.crt").read_bytes(), content_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="phone-remote-ca.crt"'})


_note_id = 0


def notify(title, body=None):
    """Show one persistent desktop notification, replacing the previous one so an old code is never on screen.
    With body=None, close it instead."""
    global _note_id
    base = ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications"]
    try:
        if body is None:
            if _note_id:
                subprocess.run(base + ["--method", "org.freedesktop.Notifications.CloseNotification", str(_note_id)],
                               capture_output=True, timeout=3)
            _note_id = 0
            return
        out = subprocess.run(base + ["--method", "org.freedesktop.Notifications.Notify", "Phone Remote", str(_note_id),
                                     "phone", title, body, "[]", "{'urgency': <byte 2>}", str(PAIR_TTL * 1000)],
                             capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"uint32 (\d+)", out)
        _note_id = int(m.group(1)) if m else 0
    except (OSError, subprocess.TimeoutExpired):
        pass


async def pair_start(request):
    global pairing
    body = await request.json()
    name = str(body.get("name", "Phone"))[:40]
    code = f"{secrets.randbelow(10**6):06d}"
    pairing = {"id": secrets.token_urlsafe(12), "code": code, "name": name,
               "expires": time.time() + PAIR_TTL, "tries": 0}
    notify(f"{name} wants to connect", f"Code: {code}  (valid 2 min, sent {time.strftime('%H:%M:%S')})")
    print(f"pairing requested by {name!r}, code {code}", flush=True)
    return web.json_response({"id": pairing["id"], "ttl": PAIR_TTL})


async def pair_finish(request):
    global pairing
    body = await request.json()
    p = pairing
    if not p or body.get("id") != p["id"] or time.time() > p["expires"]:
        return web.json_response({"error": "Pairing expired. Start again."}, status=410)
    p["tries"] += 1
    got = re.sub(r"\D", "", str(body.get("code", "")))
    if not secrets.compare_digest(got, p["code"]):
        print(f"wrong code: got {len(got)} digits, {sum(a == b for a, b in zip(got, p['code']))}/6 matching", flush=True)
        if p["tries"] >= PAIR_TRIES:
            pairing = None
            notify(None)
            return web.json_response({"error": "Too many wrong codes. Start again."}, status=410)
        return web.json_response({"error": "Wrong code."}, status=403)
    pairing = None
    notify(None)
    token = secrets.token_urlsafe(32)
    devices = load(DEVICES, [])
    devices.append({"name": p["name"], "hash": digest(token), "created": int(time.time())})
    save(DEVICES, devices)
    print(f"paired {p['name']!r}", flush=True)
    return web.json_response({"token": token})


async def apps_list(request):
    if not authorized(bearer(request)):
        raise web.HTTPUnauthorized()
    apps = scan_apps()
    slots = get_slots(apps)
    pub = sorted(({"id": a["id"], "name": a["name"]} for a in apps.values()), key=lambda a: a["name"].lower())
    return web.json_response({"apps": pub, "slots": [
        {"id": s, "name": apps.get(s, {}).get("name", "(missing)")} for s in slots]})


async def slots_set(request):
    if not authorized(bearer(request)):
        raise web.HTTPUnauthorized()
    body = await request.json()
    apps = scan_apps()
    slots = get_slots(apps)
    i, app_id = int(body.get("slot", -1)), body.get("id")
    if not 0 <= i < 4 or app_id not in apps:
        raise web.HTTPBadRequest()
    slots[i] = app_id
    save(SLOTS, slots)
    return web.json_response({"ok": True})


async def windows_list(request):
    if not authorized(bearer(request)):
        raise web.HTTPUnauthorized()
    return web.json_response({"windows": open_windows()})


async def window_action(request):
    """Bring one window to the front, or close it like its title-bar X."""
    if not authorized(bearer(request)):
        raise web.HTTPUnauthorized()
    w = (await request.json()).get("id")
    if w not in {x["id"] for x in xin().windows()}:
        raise web.HTTPNotFound()
    getattr(xin(), request.match_info["action"])(w)
    return web.json_response({"ok": True})


async def shutdown(request):
    """Close every app window like its X button, wait for them to go, then power off.
    If an app stays open (usually a "save changes?" prompt), don't power off: report it, unless force is set."""
    if not authorized(bearer(request)):
        raise web.HTTPUnauthorized()
    force = bool((await request.json()).get("force"))
    if not force:
        for w in xin().windows():
            xin().close(w["id"])
        left = []
        for _ in range(CLOSE_WAIT):
            await asyncio.sleep(1)
            left = open_windows()
            if not left:
                break
        if left:
            print("shutdown held, still open:", [w["name"] for w in left], flush=True)
            return web.json_response({"stuck": left})
    print("powering off" + (" (forced)" if force else ""), flush=True)
    asyncio.get_running_loop().call_later(1, poweroff)  # let this reply reach the phone first
    return web.json_response({"ok": True})


async def icon(request):
    app = scan_apps().get(request.match_info["id"])
    f = icon_file(app["icon"]) if app else None
    if not f:
        raise web.HTTPNotFound()
    return web.FileResponse(f, headers={"Cache-Control": "max-age=86400"})


# --- clipboard and files between laptop and phone ----------------------------

def toast(title, body=""):
    subprocess.Popen(["notify-send", "-a", "Phone Remote", "-i", "phone", title, body],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def downloads_dir():
    try:
        d = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        d = ""
    return Path(d) if d and d != str(Path.home()) else Path.home() / "Downloads"


def free_name(folder, name):
    """A safe file name in folder that doesn't overwrite anything: "a.jpg", then "a (1).jpg", ..."""
    name = Path(name.replace("\\", "/")).name.strip().lstrip(".") or "file"
    stem, suffix = Path(name).stem, Path(name).suffix
    dest, n = folder / name, 1
    while dest.exists():
        dest, n = folder / f"{stem} ({n}){suffix}", n + 1
    return dest


def set_laptop_clipboard(args, data=None):
    """Start a clip.py helper that owns the clipboard until something else is copied."""
    p = subprocess.Popen(["/usr/bin/python3", str(CLIP), *args], stdin=subprocess.PIPE, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.stdin.write(data or b"")
    p.stdin.close()


async def send_to_phone():
    """Alt+C on the laptop: copy what's selected, then hand it to the phone."""
    global outbox, outbox_paths
    x = xin()
    for _ in range(30):  # the hotkey fires while Alt is still down, which would turn Ctrl+C into Ctrl+Alt+C
        if not x.keys_down(["Alt_L", "Alt_R"]):
            break
        await asyncio.sleep(0.05)
    cls = x.active_class().lower()
    if cls in XTERMS:
        which = "primary"
    else:
        x.combo(["ctrl", "shift", "c"] if cls in TERMINALS else ["ctrl", "c"])
        which = "clipboard"
        await asyncio.sleep(0.3)
    OUTBOX_DIR.mkdir(mode=0o700, exist_ok=True)
    for f in OUTBOX_DIR.glob("clip-*.png"):
        f.unlink()
    oid = secrets.token_hex(4)
    img = OUTBOX_DIR / f"clip-{oid}.png"
    proc = await asyncio.create_subprocess_exec("/usr/bin/python3", str(CLIP), "get", which, str(img),
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        got = json.loads((await asyncio.wait_for(proc.communicate(), 10))[0] or b"{}")
    except (asyncio.TimeoutError, ValueError):
        proc.kill()
        got = {}
    kind = got.get("kind")
    if kind == "text":
        outbox, outbox_paths = {"id": oid, "kind": "text", "text": got["text"]}, []
        what = got["text"].strip().splitlines()[0][:80] if got["text"].strip() else "(blank text)"
    elif kind in ("image", "files"):
        paths = [Path(p) for p in (got["paths"] if kind == "files" else [got["path"]]) if Path(p).is_file()]
        if not paths:
            toast("Nothing sent to phone", "Folders can't be sent, only files.")
            return
        names = ["laptop-clipboard.png"] if kind == "image" else [p.name for p in paths]
        outbox_paths = paths
        outbox = {"id": oid, "kind": kind, "files": [{"name": n, "size": p.stat().st_size} for n, p in zip(names, paths)]}
        what = "Picture" if kind == "image" else ", ".join(names)
    else:
        toast("Nothing sent to phone", "Select some text or files first, then press Alt+C.")
        return
    for ws in list(clients):
        await ws.send_json({"t": "outbox", "item": outbox})
    toast("Sent to phone" if clients else "Waiting for phone: open the remote to get it", what)
    print(f"Alt+C: sent {kind} to {len(clients)} phone(s)", flush=True)


async def outbox_file(request):
    if not authorized(bearer(request)):
        raise web.HTTPUnauthorized()
    i = int(request.match_info["i"])
    if not outbox or request.match_info["id"] != outbox["id"] or not 0 <= i < len(outbox_paths):
        raise web.HTTPNotFound()
    name = outbox["files"][i]["name"].replace('"', "")
    return web.FileResponse(outbox_paths[i], headers={"Content-Disposition": f'attachment; filename="{name}"'})


async def clipboard_in(request):
    """Phone -> laptop clipboard: plain text, or a picture."""
    dev = authorized(bearer(request))
    if not dev:
        raise web.HTTPUnauthorized()
    data = await request.read()
    if request.content_type.startswith("image/"):
        OUTBOX_DIR.mkdir(mode=0o700, exist_ok=True)
        f = OUTBOX_DIR / "from-phone"
        f.write_bytes(data)
        set_laptop_clipboard(["set-image", str(f)])
        what = "Picture"
    else:
        set_laptop_clipboard(["set-text"], data)
        what = data.decode("utf-8", "replace").strip()[:80]
    toast(f"Copied from {dev['name']}: Ctrl+V to paste", what)
    return web.json_response({"ok": True})


async def upload(request):
    """Phone -> laptop files (photos, documents), saved in Downloads."""
    dev = authorized(bearer(request))
    if not dev:
        raise web.HTTPUnauthorized()
    folder = downloads_dir()
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    async for part in await request.multipart():
        if not part.filename:
            continue
        dest = free_name(folder, part.filename)
        tmp = dest.with_name(f".{dest.name}.part")
        try:
            with open(tmp, "wb") as fh:
                while chunk := await part.read_chunk(1 << 20):
                    fh.write(chunk)
            dest = free_name(folder, dest.name)  # re-check: another upload may have taken the name meanwhile
            tmp.rename(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        saved.append(dest.name)
    if saved:
        toast(f"{len(saved)} file{'s' * (len(saved) > 1)} from {dev['name']} in {folder.name}", ", ".join(saved)[:200])
        print(f"received {len(saved)} file(s) into {folder}", flush=True)
    return web.json_response({"saved": saved, "folder": str(folder)})


# --- live control over WebSocket -------------------------------------------

def handle(msg):
    t = msg.get("t")
    x = xin()
    if t == "move":
        x.move(msg["dx"], msg["dy"])
    elif t == "click":
        x.click(msg.get("b", 1))
    elif t == "button":
        x.button(msg.get("b", 1), msg["down"])
    elif t == "scroll":
        x.scroll(msg.get("dx", 0), msg.get("dy", 0))
    elif t == "text":
        x.type_text(str(msg["s"])[:2000])
    elif t == "keys":
        x.combo([str(k) for k in msg["keys"]][:5])
    elif t == "launch":
        app = scan_apps().get(msg.get("id"))
        if app:
            subprocess.Popen(["gio", "launch", app["path"]], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    try:
        first = await ws.receive_json(timeout=10)
    except (asyncio.TimeoutError, TypeError, ValueError):
        await ws.close()
        return ws
    dev = authorized(first.get("token") if first.get("t") == "auth" else None)
    if not dev:
        await ws.send_json({"t": "unauthorized"})
        await ws.close()
        return ws
    await ws.send_json({"t": "ready", "host": os.uname().nodename})
    if outbox:
        await ws.send_json({"t": "outbox", "item": outbox})
    clients.add(ws)
    try:
        await read_loop(ws)
    finally:
        clients.discard(ws)
    return ws


async def read_loop(ws):
    async for m in ws:
        if m.type != WSMsgType.TEXT:
            continue
        try:
            handle(json.loads(m.data))
        except Exception as e:  # keep the connection alive on a bad message or X hiccup
            print("input error:", e, flush=True)
            await ws.send_json({"t": "error", "msg": str(e)})


def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent; this just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


async def publish_name(app):
    """Keep <hostname>-remote.local pointing at the current LAN IP over mDNS, so the phone link never changes."""
    name = f"{os.uname().nodename}-remote.local"
    proc, ip = None, None

    async def loop():
        nonlocal proc, ip
        while True:
            now = lan_ip()
            if now != ip or (proc and proc.returncode is not None):
                if proc and proc.returncode is None:
                    proc.terminate()
                ip, proc = now, None
                if ip:
                    proc = await asyncio.create_subprocess_exec(
                        "avahi-publish", "-a", "-R", name, ip,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    print(f"mDNS: {name} -> {ip}", flush=True)
            await asyncio.sleep(20)

    task = asyncio.create_task(loop())
    yield
    task.cancel()
    if proc and proc.returncode is None:
        proc.terminate()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--revoke-all", action="store_true")
    args = ap.parse_args()
    if args.list_devices:
        for d in load(DEVICES, []):
            print(d["name"], time.strftime("%Y-%m-%d %H:%M", time.localtime(d["created"])))
        return
    if args.revoke_all:
        save(DEVICES, [])
        print("all paired devices removed")
        return
    import logging
    import ssl
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    crt, key = ensure_tls()
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(crt, key)
    app = web.Application(client_max_size=64 << 20)  # a pasted picture can be several MB
    app.add_routes([
        web.get("/", index),
        web.get("/ca.crt", ca_cert),
        web.post("/api/pair/start", pair_start),
        web.post("/api/pair/finish", pair_finish),
        web.get("/api/apps", apps_list),
        web.post("/api/slots", slots_set),
        web.get("/api/icon/{id}", icon),
        web.get("/api/windows", windows_list),
        web.post("/api/windows/{action:activate|close}", window_action),
        web.post("/api/shutdown", shutdown),
        web.get("/api/outbox/{id}/{i:\\d+}", outbox_file),
        web.post("/api/clipboard", clipboard_in),
        web.post("/api/upload", upload),
        web.get("/ws", ws_handler),
        web.static("/static", HERE / "static"),
    ])
    app.cleanup_ctx.append(publish_name)

    async def on_alt_c(app):
        # The Alt+C desktop shortcut runs: systemctl --user kill --kill-who=main -s SIGUSR1 phone-remote
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGUSR1, lambda: loop.create_task(send_to_phone()))
    app.on_startup.append(on_alt_c)
    web.run_app(app, host=["0.0.0.0", "::"], port=args.port, ssl_context=ctx, print=lambda *_: print(
        f"Phone Remote on https://{os.uname().nodename}-remote.local:{args.port}/", flush=True))


if __name__ == "__main__":
    main()
