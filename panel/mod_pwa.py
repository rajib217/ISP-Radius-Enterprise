"""Install the panel (and the customer portal) as an app: web app manifests, icons made from the uploaded logo,
a service worker (fast static files, an offline page) and the launch splash screen (static/js/pwa.js + _pwa_head.html).

  /manifest.webmanifest          the panel app        (starts on the dashboard)
  /portal/manifest.webmanifest   the customer app     (starts on /portal)
  /pwa/icon-<size>[-maskable].png  192 / 512 / 180 (Apple) icons: the logo on a white tile; Pillow resizes it.
                                 Without Pillow or a logo, the panel's ring mark is drawn instead.
  /sw.js                         the service worker (scope /). Pages are never cached (they carry customer data and
                                 form tokens); only /static and the icons are, and /offline is shown with no network.
  /offline                       "You are offline" page
"""
import hashlib
import json
import math
import os
import struct
import zlib

from flask import Response, abort, render_template, url_for

import core
from core import cfg, route

APP_BG = "#0f172a"          # splash background and the phone's status bar colour
ICON_SIZES = (180, 192, 512)


# ===================================================================== icons
def _png(width, height, rgba):
    """A PNG file from raw RGBA bytes (no image library needed)."""
    raw = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _ring_icon(size, maskable):
    """The panel's mark (two rings and a dot) on a white tile, drawn pixel by pixel with soft edges."""
    c = (size - 1) / 2.0
    tile_r = size * 0.22                      # rounded corners of the tile (a maskable icon fills the square)
    rings = [(size * 0.31, size * 0.036, (37, 99, 235)), (size * 0.18, size * 0.036, (16, 185, 129))]
    dot = size * 0.06
    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            if maskable:
                a_tile = 1.0
            else:                              # rounded square, anti-aliased corners
                qx, qy = max(abs(x - c) - (c - tile_r), 0), max(abs(y - c) - (c - tile_r), 0)
                a_tile = min(max(tile_r - math.hypot(qx, qy) + 0.5, 0.0), 1.0)
            r, g, b = 255, 255, 255
            d = math.hypot(x - c, y - c)
            for radius, half, col in rings:
                a = min(max(half - abs(d - radius) + 0.5, 0.0), 1.0)
                if a:
                    r, g, b = (int(r + (col[0] - r) * a), int(g + (col[1] - g) * a), int(b + (col[2] - b) * a))
            a = min(max(dot - d + 0.5, 0.0), 1.0)
            if a:
                r, g, b = (int(r + (37 - r) * a), int(g + (99 - g) * a), int(b + (235 - b) * a))
            i = (y * size + x) * 4
            out[i:i + 4] = bytes((r, g, b, int(255 * a_tile)))
    return _png(size, size, bytes(out))


def _logo_icon(path, size, maskable):
    """The uploaded logo centred on a white tile (Pillow), or None when that is not possible."""
    try:
        from PIL import Image, ImageDraw
        logo = Image.open(path).convert("RGBA")
    except Exception:
        return None
    tile = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    if not maskable:                           # rounded square with transparent corners
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.22), fill=255)
        tile.putalpha(mask)
    box = int(size * (0.62 if maskable else 0.78))   # maskable: keep the logo inside the safe circle
    logo.thumbnail((box, box), Image.LANCZOS)
    tile.alpha_composite(logo, ((size - logo.width) // 2, (size - logo.height) // 2))
    import io
    buf = io.BytesIO()
    tile.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _logo_path():
    import mod_settings
    return mod_settings._brand_file("logo")


def icon_key():
    """Changes when the logo changes, so phones fetch the new icons."""
    p = _logo_path()
    return ("logo-%d" % int(os.path.getmtime(p))) if p else "mark"


def icon_bytes(size, maskable):
    key = icon_key()
    folder = os.path.join(core.DATA_DIR, "branding", "pwa")
    path = os.path.join(folder, "icon-%d%s-%s.png" % (size, "-m" if maskable else "", key))
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            return fh.read()
    logo = _logo_path()
    data = (_logo_icon(logo, size, maskable) if logo else None) or _ring_icon(size, maskable)
    try:
        os.makedirs(folder, exist_ok=True)
        for old in os.listdir(folder):          # drop icons of an older logo
            if old.startswith("icon-%d%s-" % (size, "-m" if maskable else "")):
                os.remove(os.path.join(folder, old))
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError:
        pass
    return data


@route("/pwa/<name>.png")
def pwa_icon(name):
    parts = name.split("-")
    if len(parts) not in (2, 3) or parts[0] != "icon" or not parts[1].isdigit() or int(parts[1]) not in ICON_SIZES \
            or (len(parts) == 3 and parts[2] != "maskable"):
        abort(404)
    return Response(icon_bytes(int(parts[1]), len(parts) == 3), mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})


# ===================================================================== manifests
def _icon(size, maskable=False):
    return {"src": url_for("pwa_icon", name="icon-%d%s" % (size, "-maskable" if maskable else ""), v=icon_key()),
            "sizes": "%dx%d" % (size, size), "type": "image/png", "purpose": "maskable" if maskable else "any"}


def manifest(kind):
    s = cfg()
    isp = (s.get("isp_name") or "ISP").strip()
    portal = kind == "portal"
    data = {
        "id": "/portal" if portal else "/",
        "name": isp if portal else "%s Panel" % isp,
        "short_name": isp[:12] if portal else ("%s Panel" % isp)[:12],
        "description": ("Your internet account: usage, invoices and bKash payment." if portal
                        else "RADIUS panel: customers, payments, routers and reports."),
        "start_url": "/portal?source=pwa" if portal else "/?source=pwa",
        "scope": "/portal" if portal else "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": APP_BG,
        "theme_color": APP_BG,
        "lang": "en",
        "icons": [_icon(192), _icon(512), _icon(512, True)],
    }
    return Response(json.dumps(data, ensure_ascii=False, indent=1), mimetype="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


@route("/manifest.webmanifest")
def pwa_manifest():
    return manifest("panel")


@route("/portal/manifest.webmanifest")
def portal_manifest():
    return manifest("portal")


# ===================================================================== service worker and offline page
SW = r"""/* ISP panel service worker (written by mod_pwa.py). Pages are never cached: only static files and icons,
   plus the offline page shown when there is no network. */
const CACHE = "isp-panel-__VERSION__";
const PRECACHE = __PRECACHE__;
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith("isp-panel-") && k !== CACHE)
    .map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
function isPortal(c) {            /* a window of the customer app, not a staff panel window */
  try { const u = new URL(c.url); return u.origin === self.location.origin && (u.pathname === "/portal" || u.pathname.startsWith("/portal/")); }
  catch (x) { return false; }
}
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { body: e.data ? e.data.text() : "" }; }
  const title = d.title || "নোটিফিকেশন";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    list.filter(isPortal).forEach((c) => c.postMessage({ type: "push", title: title, body: d.body || "", unread: d.unread }));
    if (typeof d.unread === "number" && self.navigator.setAppBadge) { self.navigator.setAppBadge(d.unread).catch(() => {}); }
    return self.registration.showNotification(title, {
      body: d.body || "", icon: "__ICON__", tag: d.tag, renotify: true, silent: false,
      vibrate: [180, 80, 180], data: { url: d.url || "/portal/notifications" } });
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/portal/notifications";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const c of list) {
      if (isPortal(c) && "focus" in c) { if (c.navigate) c.navigate(url); return c.focus(); }
    }
    return self.clients.openWindow(url);
  }));
});
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(() => caches.match("/offline")));
    return;
  }
  if (url.pathname.startsWith("/static/") || url.pathname.startsWith("/pwa/")) {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      return res;
    })));
  }
});
"""


@route("/sw.js")
def pwa_sw():
    from app import asset_url
    files = [asset_url("css/app.css"), asset_url("js/app.js"), asset_url("js/pwa.js"), asset_url("js/charts.js")]
    pre = ["/offline"] + files + [url_for("pwa_icon", name="icon-192", v=icon_key()),
                                  url_for("pwa_icon", name="icon-512", v=icon_key())]
    version = hashlib.sha1("|".join(pre).encode()).hexdigest()[:12]
    body = SW.replace("__VERSION__", version).replace("__PRECACHE__", json.dumps(pre)).replace(
        "__ICON__", url_for("pwa_icon", name="icon-192", v=icon_key()))
    return Response(body, mimetype="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@route("/offline")
def pwa_offline():
    s = cfg()
    return render_template("offline.html", isp=s.get("isp_name") or "", icon=url_for("pwa_icon", name="icon-192", v=icon_key()))
