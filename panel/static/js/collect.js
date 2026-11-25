/* Bill collector pages (mod_collect.py): search suggestions, "near me", the dues-nearby panel on the map, the visit box.
   No library. Works with the keyboard (arrows, Enter, Esc) and on touch. */
(function () {
  "use strict";
  function esc(v) { return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function money(n) { return Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function metres(m) { return m >= 1000 ? (m / 1000).toFixed(1) + " km" : m + " m"; }
  var CUR = (document.body.dataset.cur || document.querySelector("[data-cur]") && document.querySelector("[data-cur]").dataset.cur) || "";

  /* ---------- search with suggestions (input[data-suggest]) ---------- */
  document.querySelectorAll("input[data-suggest]").forEach(function (input) {
    var box = document.createElement("div"), mode = input.dataset.suggestMode || "list", timer = null, items = [], active = -1, seq = 0;
    box.className = "cl-sugg"; box.hidden = true; box.setAttribute("role", "listbox"); box.id = "sugg-" + Math.random().toString(36).slice(2);
    input.parentNode.appendChild(box);
    input.setAttribute("role", "combobox"); input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false"); input.setAttribute("aria-controls", box.id); input.setAttribute("autocomplete", "off");
    function close() { box.hidden = true; active = -1; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); }
    function mark(i) {
      active = i;
      box.querySelectorAll(".cl-sugg-i").forEach(function (el, k) { el.classList.toggle("on", k === i); if (k === i) { input.setAttribute("aria-activedescendant", el.id); el.scrollIntoView({ block: "nearest" }); } });
    }
    function pick(it) {
      close();
      if (it.kind === "area") {
        if (mode === "map") { input.value = ""; document.dispatchEvent(new CustomEvent("cl:area", { detail: it.name })); }
        else { var u = new URL(location.href); u.searchParams.set("area", it.name); u.searchParams.delete("q"); location.href = u.toString(); }
        return;
      }
      if (it.kind === "all") { input.form && input.form.submit(); return; }
      if (mode === "map") {
        input.value = it.n || it.u;
        document.dispatchEvent(new CustomEvent("cl:customer", { detail: it }));
      } else { location.href = it.url; }
    }
    function render(data, term) {
      items = [];
      var html = [];
      (data.areas || []).forEach(function (a) { items.push({ kind: "area", name: a.name, n: a.n }); });
      (data.subs || []).forEach(function (s) { s.kind = "sub"; items.push(s); });
      if (mode !== "map" && term) items.push({ kind: "all", term: term });
      if (!items.length) { box.innerHTML = '<div class="cl-sugg-none">No customer or area matches "' + esc(term) + '"</div>'; box.hidden = false; input.setAttribute("aria-expanded", "true"); return; }
      var lastKind = "";
      items.forEach(function (it, i) {
        if (it.kind !== lastKind) {
          if (it.kind === "area") html.push('<div class="cl-sugg-h">Areas</div>');
          if (it.kind === "sub") html.push('<div class="cl-sugg-h">Customers</div>');
          lastKind = it.kind;
        }
        var id = box.id + "-" + i;
        if (it.kind === "area") {
          html.push('<div class="cl-sugg-i" role="option" id="' + id + '" data-i="' + i + '"><span class="cl-sugg-ic a"><svg class="i"><use href="#i-pin"/></svg></span>' +
            '<span class="cl-sugg-t"><b>' + esc(it.name) + '</b><small>' + it.n + ' customer' + (it.n === 1 ? '' : 's') + ' owe here</small></span><em>' + (mode === "map" ? "Show" : "Filter") + '</em></div>');
        } else if (it.kind === "sub") {
          var due = it.due > 0 ? '<em class="due">' + esc(CUR) + ' ' + money(it.due) + '</em>' : '<em class="ok">Paid up</em>';
          var where = [it.code, it.u, it.a, it.ph].filter(Boolean).map(esc).join(" · ");
          var nomap = mode === "map" && it.lat == null ? ' <span class="cl-sugg-nomap">no map location</span>' : '';
          html.push('<div class="cl-sugg-i" role="option" id="' + id + '" data-i="' + i + '"><span class="cl-sugg-ic">' + esc((it.n || it.u).slice(0, 2).toUpperCase()) + '</span>' +
            '<span class="cl-sugg-t"><b>' + esc(it.n || it.u) + nomap + '</b><small>' + where + '</small></span>' + due + '</div>');
        } else {
          html.push('<div class="cl-sugg-i all" role="option" id="' + id + '" data-i="' + i + '"><span class="cl-sugg-ic"><svg class="i"><use href="#i-search"/></svg></span><span class="cl-sugg-t"><b>Show all results for "' + esc(it.term) + '"</b></span></div>');
        }
      });
      box.innerHTML = html.join(""); box.hidden = false; input.setAttribute("aria-expanded", "true"); active = -1;
    }
    input.addEventListener("input", function () {
      clearTimeout(timer);
      var term = input.value.trim();
      if (term.length < 2) { close(); return; }
      timer = setTimeout(function () {
        var my = ++seq;
        fetch(input.dataset.suggest + "?q=" + encodeURIComponent(term), { credentials: "same-origin" })
          .then(function (r) { return r.json(); })
          .then(function (d) { if (my === seq) render(d, term); }).catch(function () {});
      }, 180);
    });
    input.addEventListener("keydown", function (e) {
      if (box.hidden) return;
      if (e.key === "ArrowDown") { e.preventDefault(); mark(Math.min(items.length - 1, active + 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); mark(Math.max(0, active - 1)); }
      else if (e.key === "Enter" && active >= 0) { e.preventDefault(); pick(items[active]); }
      else if (e.key === "Escape") { close(); }
    });
    box.addEventListener("mousedown", function (e) { e.preventDefault(); });      // keep focus in the box
    box.addEventListener("click", function (e) { var el = e.target.closest(".cl-sugg-i"); if (el) pick(items[+el.dataset.i]); });
    input.addEventListener("blur", function () { setTimeout(close, 120); });
    input.addEventListener("focus", function () { if (input.value.trim().length >= 2 && box.innerHTML) { box.hidden = false; input.setAttribute("aria-expanded", "true"); } });
  });

  /* ---------- where am I (geolocation, with clear messages) ---------- */
  function locate(btn, done) {
    if (!navigator.geolocation) { alert("This device cannot share its location."); return; }
    var label = btn ? btn.innerHTML : "";
    if (btn) { btn.disabled = true; btn.innerHTML = "Finding you…"; }
    navigator.geolocation.getCurrentPosition(function (pos) {
      if (btn) { btn.disabled = false; btn.innerHTML = label; }
      done(pos.coords.latitude, pos.coords.longitude);
    }, function (err) {
      if (btn) { btn.disabled = false; btn.innerHTML = label; }
      alert(err.code === 1 ? "Location is blocked. Allow location for this site in the browser settings, then try again."
                           : "Could not find your position. Try again in the open, with GPS on.");
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
  }

  /* the due list: "Nearest to me" sorts by distance from where the collector stands */
  var sortSel = document.querySelector("[data-cl-sort]");
  if (sortSel) sortSel.addEventListener("change", function () {
    var u = new URL(location.href);
    u.searchParams.set("sort", sortSel.value);
    if (sortSel.value !== "near") { u.searchParams.delete("lat"); u.searchParams.delete("lng"); location.href = u.toString(); return; }
    locate(null, function (lat, lng) { u.searchParams.set("lat", lat.toFixed(6)); u.searchParams.set("lng", lng.toFixed(6)); location.href = u.toString(); });
  });

  /* ---------- the visit box (one dialog, filled per customer) ---------- */
  var vd = document.getElementById("visit-dlg");
  if (vd) {
    var vf = vd.querySelector("form"), promise = vd.querySelector("[data-visit-date]");
    document.addEventListener("click", function (e) {
      var b = e.target.closest("[data-visit]");
      if (!b) return;
      vf.action = b.dataset.visit;
      vd.querySelector("[data-visit-who]").textContent = b.dataset.who || "";
      vf.reset();
      if (promise) promise.hidden = true;
      if (vd.showModal) vd.showModal(); else vd.setAttribute("open", "");
    });
    vf.querySelectorAll("input[name=outcome]").forEach(function (r) {
      r.addEventListener("change", function () {     // a come-back date for "promised" (required) and "will pay later"
        var v = (vf.querySelector("input[name=outcome]:checked") || {}).value || "";
        if (promise) promise.hidden = !(v === "promised" || v === "visit_later");
        var d = vf.querySelector("input[name=follow_up]"); if (d) d.required = v === "promised";
      });
    });
  }

  /* ---------- the map page ---------- */
  var mapEl = document.getElementById("cmap"), panel = document.querySelector("[data-near-panel]");
  if (!mapEl || !panel) return;
  var radiusSel = document.querySelector("[data-near-r]"), list = panel.querySelector("[data-near-list]"), head = panel.querySelector("[data-near-head]");
  var lastPoint = null;
  function near(lat, lng, label) {
    lastPoint = { lat: lat, lng: lng, label: label };
    var r = +(radiusSel ? radiusSel.value : 1000);
    window.cmap && window.cmap.ring(lat, lng, r);
    window.cmap && window.cmap.fitAround(lat, lng, r);
    head.innerHTML = "Looking for dues within " + metres(r) + " of " + esc(label) + "…";
    panel.hidden = false;
    fetch(mapEl.dataset.near + "?lat=" + lat + "&lng=" + lng + "&r=" + r, { credentials: "same-origin" })
      .then(function (x) { return x.json(); })
      .then(function (d) {
        if (!d.ok) { head.textContent = d.why || "Could not look it up."; list.innerHTML = ""; return; }
        head.innerHTML = '<b>' + d.n + ' customer' + (d.n === 1 ? '' : 's') + ' owe ' + esc(CUR) + ' ' + money(d.total) + '</b><span>within ' + metres(d.radius) + ' of ' + esc(label) + '</span>';
        list.innerHTML = d.rows.length ? d.rows.map(function (s) {
          return '<li><button type="button" class="cl-near-go" data-id="' + s.id + '" aria-label="Show ' + esc(s.n || s.u) + ' on the map"><span class="cl-near-m">' + metres(s.m) + '</span></button>' +
            '<div class="cl-near-t"><b>' + esc(s.n || s.u) + '</b><small>' + [s.code, s.a, s.late ? s.late + "d overdue" : ""].filter(Boolean).map(esc).join(" · ") + '</small></div>' +
            '<div class="cl-near-r"><b>' + esc(CUR) + ' ' + money(s.due) + '</b><span>' +
            (s.ph ? '<a class="btn sm icon" href="tel:' + esc(s.ph) + '" aria-label="Call"><svg class="i"><use href="#i-phone"/></svg></a>' : '') +
            '<a class="btn sm primary" href="' + esc(s.url) + '">Collect</a></span></div></li>';
        }).join("") : '<li class="cl-near-none">Nobody owes within this distance. Try a wider circle.</li>';
      }).catch(function () { head.textContent = "Could not look it up. Check the connection and try again."; });
  }
  list.addEventListener("click", function (e) { var b = e.target.closest(".cl-near-go"); if (b && window.cmap) window.cmap.open(+b.dataset.id); });
  if (radiusSel) radiusSel.addEventListener("change", function () { if (lastPoint) near(lastPoint.lat, lastPoint.lng, lastPoint.label); });
  var meBtn = document.querySelector("[data-near-me]");
  if (meBtn) meBtn.addEventListener("click", function () {
    locate(meBtn, function (lat, lng) { window.cmap && window.cmap.me(lat, lng); near(lat, lng, "you"); });
  });
  mapEl.addEventListener("cmap:near", function (e) { near(e.detail.lat, e.detail.lng, e.detail.p.n || e.detail.p.u); });
  var closeBtn = panel.querySelector("[data-near-close]");
  if (closeBtn) closeBtn.addEventListener("click", function () { panel.hidden = true; lastPoint = null; window.cmap && window.cmap.ring(null); });
  var msg = document.querySelector("[data-map-msg]");
  document.addEventListener("cl:customer", function (e) {
    var s = e.detail;
    if (s.lat == null || !window.cmap || !window.cmap.open(s.id)) {
      if (msg) { msg.innerHTML = esc(s.n || s.u) + ' has no saved map location. <a href="' + esc(s.url) + '">Open their payment page</a>.'; msg.hidden = false; }
      return;
    }
    if (msg) msg.hidden = true;
  });
  document.addEventListener("cl:area", function (e) {
    var sel = document.querySelector("[data-cmap-area]");
    if (sel) { sel.value = e.detail; sel.dispatchEvent(new Event("change")); }
  });
})();
