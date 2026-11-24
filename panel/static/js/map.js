/* Customer map (Subscribers > Map): a small slippy map written here (no library, no CDN). OpenStreetMap tiles are the
   only thing loaded from outside; without internet the markers still show on a plain grid.
   Drag or swipe to move, wheel / pinch / + − to zoom, tap a dot for the customer card. */
(function () {
  "use strict";
  var el = document.getElementById("cmap");
  if (!el) return;
  var TILE = 256, MINZ = 3, MAXZ = 19;
  var tileUrl = function (z, x, y) { return "https://tile.openstreetmap.org/" + z + "/" + x + "/" + y + ".png"; };
  var tilesLayer = document.createElement("div"), dotsLayer = document.createElement("div"), card = document.createElement("div");
  tilesLayer.className = "cmap-tiles"; dotsLayer.className = "cmap-dots"; card.className = "cmap-card"; card.hidden = true;
  el.insertBefore(dotsLayer, el.firstChild); el.insertBefore(tilesLayer, dotsLayer); el.appendChild(card);

  var centre = null; try { centre = JSON.parse(el.dataset.centre || "null"); } catch (e) {}
  var view = { lat: centre ? centre[0] : 23.8103, lng: centre ? centre[1] : 90.4125, z: centre ? 13 : 7 };
  var all = [], shown = [];

  /* Web Mercator: degrees <-> world pixels at zoom z */
  function project(lat, lng, z) {
    var s = TILE * Math.pow(2, z), sin = Math.sin(Math.max(Math.min(lat, 85), -85) * Math.PI / 180);
    return { x: (lng + 180) / 360 * s, y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * s };
  }
  function unproject(x, y, z) {
    var s = TILE * Math.pow(2, z), n = Math.PI - 2 * Math.PI * y / s;
    return { lat: 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n))), lng: x / s * 360 - 180 };
  }
  function size() { return { w: el.clientWidth, h: el.clientHeight }; }
  function origin() { var c = project(view.lat, view.lng, view.z), sz = size(); return { x: c.x - sz.w / 2, y: c.y - sz.h / 2 }; }

  var tileCache = {};
  function drawTiles() {
    var o = origin(), sz = size(), z = view.z, n = Math.pow(2, z), want = {};
    var x0 = Math.floor(o.x / TILE), y0 = Math.floor(o.y / TILE), x1 = Math.floor((o.x + sz.w) / TILE), y1 = Math.floor((o.y + sz.h) / TILE);
    for (var tx = x0; tx <= x1; tx++) {
      for (var ty = Math.max(y0, 0); ty <= Math.min(y1, n - 1); ty++) {
        var wx = ((tx % n) + n) % n, key = z + "/" + tx + "/" + ty, img = tileCache[key];
        if (!img) {
          img = document.createElement("img");
          img.alt = ""; img.draggable = false; img.decoding = "async"; img.referrerPolicy = "strict-origin-when-cross-origin";
          img.onerror = function () { this.style.visibility = "hidden"; };
          img.src = tileUrl(z, wx, ty);
          tileCache[key] = img; tilesLayer.appendChild(img);
        }
        img.style.transform = "translate(" + Math.round(tx * TILE - o.x) + "px," + Math.round(ty * TILE - o.y) + "px)";
        want[key] = 1;
      }
    }
    for (var k in tileCache) { if (!want[k]) { tilesLayer.removeChild(tileCache[k]); delete tileCache[k]; } }
  }

  function drawDots() {
    var o = origin(), sz = size(), html = [];
    shown.forEach(function (p, i) {
      var q = project(p.lat, p.lng, view.z), x = q.x - o.x, y = q.y - o.y;
      if (x < -20 || y < -20 || x > sz.w + 20 || y > sz.h + 20) return;
      html.push('<button type="button" class="cmap-dot s-' + p.s + '" data-i="' + i + '" style="transform:translate(' +
        Math.round(x) + 'px,' + Math.round(y) + 'px)" aria-label="' + esc(p.n || p.u) + '"></button>');
    });
    dotsLayer.innerHTML = html.join("");
  }
  function draw() { drawTiles(); drawDots(); if (!card.hidden && card._p) placeCard(card._p); }

  function esc(t) { return String(t == null ? "" : t).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  var STATE = { online: "Online", offline: "Offline", expired: "Expired", suspended: "Suspended" };
  function placeCard(p) {
    var o = origin(), q = project(p.lat, p.lng, view.z);
    card.style.transform = "translate(" + Math.round(q.x - o.x) + "px," + Math.round(q.y - o.y) + "px)";
  }
  function openCard(p) {
    card._p = p;
    card.innerHTML = '<button type="button" class="cmap-x" aria-label="Close">×</button><b>' + esc(p.n || p.u) + '</b>' +
      '<small>' + esc(p.code) + ' · ' + esc(p.u) + '</small>' +
      '<span class="cmap-st s-' + p.s + '">' + STATE[p.s] + '</span>' +
      (p.p ? '<small>' + esc(p.p) + (p.a ? ' · ' + esc(p.a) : '') + '</small>' : (p.a ? '<small>' + esc(p.a) + '</small>' : '')) +
      '<span class="cmap-acts"><a class="btn sm primary" href="' + esc(p.url) + '">Open</a>' +
      (p.ph ? '<a class="btn sm" href="tel:' + esc(p.ph) + '">Call</a>' : '') +
      '<a class="btn sm" target="_blank" rel="noopener" href="https://www.google.com/maps/dir/?api=1&destination=' + p.lat + ',' + p.lng + '">Directions</a></span>';
    card.hidden = false; placeCard(p);
  }
  card.addEventListener("click", function (e) { if (e.target.closest(".cmap-x")) { card.hidden = true; card._p = null; } });
  dotsLayer.addEventListener("click", function (e) { var b = e.target.closest(".cmap-dot"); if (b) { e.stopPropagation(); openCard(shown[+b.dataset.i]); } });

  function zoomAt(dz, px, py) {
    var nz = Math.max(MINZ, Math.min(MAXZ, view.z + dz));
    if (nz === view.z) return;
    var sz = size(); px = px == null ? sz.w / 2 : px; py = py == null ? sz.h / 2 : py;
    var o = origin(), at = unproject(o.x + px, o.y + py, view.z), a2 = project(at.lat, at.lng, nz);
    var c = unproject(a2.x - px + sz.w / 2, a2.y - py + sz.h / 2, nz);
    view = { lat: c.lat, lng: c.lng, z: nz }; draw();
  }
  function fit(points) {
    if (!points.length) { draw(); return; }
    var sz = size(), lat0 = 90, lat1 = -90, lng0 = 180, lng1 = -180;
    points.forEach(function (p) { lat0 = Math.min(lat0, p.lat); lat1 = Math.max(lat1, p.lat); lng0 = Math.min(lng0, p.lng); lng1 = Math.max(lng1, p.lng); });
    var z = MAXZ - 2;
    for (; z > MINZ; z--) {
      var a = project(lat1, lng0, z), b = project(lat0, lng1, z);
      if (b.x - a.x < sz.w - 60 && b.y - a.y < sz.h - 60) break;
    }
    view = { lat: (lat0 + lat1) / 2, lng: (lng0 + lng1) / 2, z: z }; draw();
  }

  /* drag / swipe and pinch */
  var pts = {}, last = null, pinch = null, moved = 0;
  el.addEventListener("pointerdown", function (e) {
    if (e.target.closest(".cmap-zoom, .cmap-card, .cmap-attr, .cmap-dot")) return;
    el.setPointerCapture(e.pointerId); pts[e.pointerId] = { x: e.clientX, y: e.clientY }; moved = 0;
    var ids = Object.keys(pts);
    if (ids.length === 2) { var a = pts[ids[0]], b = pts[ids[1]]; pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), z: view.z }; }
    last = { x: e.clientX, y: e.clientY };
  });
  el.addEventListener("pointermove", function (e) {
    if (!pts[e.pointerId]) return;
    pts[e.pointerId] = { x: e.clientX, y: e.clientY };
    var ids = Object.keys(pts);
    if (ids.length === 2 && pinch) {
      var a = pts[ids[0]], b = pts[ids[1]], d = Math.hypot(a.x - b.x, a.y - b.y), r = el.getBoundingClientRect();
      var want = Math.round(pinch.z + Math.log(d / pinch.d) / Math.LN2);
      if (want !== view.z) zoomAt(want - view.z, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top);
      return;
    }
    var dx = e.clientX - last.x, dy = e.clientY - last.y; last = { x: e.clientX, y: e.clientY }; moved += Math.abs(dx) + Math.abs(dy);
    var c = project(view.lat, view.lng, view.z), n = unproject(c.x - dx, c.y - dy, view.z);
    view.lat = n.lat; view.lng = n.lng; draw();
  });
  function up(e) { delete pts[e.pointerId]; if (Object.keys(pts).length < 2) pinch = null; if (!Object.keys(pts).length) last = null; }
  el.addEventListener("pointerup", up); el.addEventListener("pointercancel", up);
  el.addEventListener("wheel", function (e) {
    e.preventDefault(); var r = el.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1 : -1, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  el.addEventListener("dblclick", function (e) { if (e.target.closest(".cmap-zoom, .cmap-card")) return; var r = el.getBoundingClientRect(); zoomAt(1, e.clientX - r.left, e.clientY - r.top); });
  el.addEventListener("keydown", function (e) {
    var step = { ArrowLeft: [-80, 0], ArrowRight: [80, 0], ArrowUp: [0, -80], ArrowDown: [0, 80] }[e.key];
    if (step) { e.preventDefault(); var c = project(view.lat, view.lng, view.z), n = unproject(c.x + step[0], c.y + step[1], view.z); view.lat = n.lat; view.lng = n.lng; draw(); }
    else if (e.key === "+" || e.key === "=") zoomAt(1); else if (e.key === "-") zoomAt(-1);
  });
  document.querySelectorAll("[data-cmap-zoom]").forEach(function (b) { b.addEventListener("click", function () { zoomAt(+b.dataset.cmapZoom); }); });
  var fitBtn = document.querySelector("[data-cmap-fit]");
  if (fitBtn) fitBtn.addEventListener("click", function () { fit(shown); });

  /* filters */
  var qBox = document.querySelector("[data-cmap-q]"), areaSel = document.querySelector("[data-cmap-area]");
  function filter(refit) {
    var t = (qBox && qBox.value || "").trim().toLowerCase(), area = areaSel ? areaSel.value : "", on = {};
    document.querySelectorAll("[data-cmap-s]").forEach(function (c) { on[c.dataset.cmapS] = c.checked; });
    var n = { online: 0, offline: 0, expired: 0, suspended: 0 };
    shown = all.filter(function (p) {
      if (area && p.a !== area) return false;
      if (t && (p.n + " " + p.u + " " + p.code + " " + p.ph).toLowerCase().indexOf(t) < 0) return false;
      n[p.s]++;
      return !!on[p.s];
    });
    Object.keys(n).forEach(function (k) { var b = document.querySelector('[data-cmap-n="' + k + '"]'); if (b) b.textContent = n[k].toLocaleString(); });
    card.hidden = true; card._p = null;
    if (refit) fit(shown); else drawDots();
  }
  var tmr = null;
  if (qBox) qBox.addEventListener("input", function () { clearTimeout(tmr); tmr = setTimeout(function () { filter(true); }, 250); });
  if (areaSel) areaSel.addEventListener("change", function () { filter(true); });
  document.querySelectorAll("[data-cmap-s]").forEach(function (c) { c.addEventListener("change", function () { filter(false); }); });
  window.addEventListener("resize", draw);

  draw();
  fetch(el.dataset.src, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (rows) {
    all = rows || []; filter(true);
  }).catch(function () { draw(); });
  // a live map (data-refresh="seconds") fetches the points again without moving the view
  var every = parseInt(el.dataset.refresh, 10);
  if (every > 0) setInterval(function () {
    if (document.hidden) return;
    fetch(el.dataset.src, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (rows) {
      all = rows || []; filter(false);
    }).catch(function () {});
  }, every * 1000);
})();
