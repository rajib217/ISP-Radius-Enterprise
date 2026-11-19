/* Logs & System Health: polls /api/noc every 3 seconds for rows newer than the newest one shown. */
(function () {
  "use strict";
  var root = document.getElementById("noc");
  if (!root) return;
  var API = root.dataset.api, EVERY = 3000, KEEP = 200;
  var lastAuth = 0, lastAudit = 0, paused = false, filter = "all", timer = null, busy = false;
  var authBody = document.querySelector("#noc-auth tbody"), auditBody = document.querySelector("#noc-audit tbody");
  var q = document.getElementById("noc-q");

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function td(text, cls, label) { var c = el("td", cls, text); if (label) c.setAttribute("data-label", label); return c; }
  function badge(text, tone) { var b = el("span", "badge " + tone, text); return b; }
  function dur(s) {
    if (s === null || s === undefined) return "—";
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? d + "d " + h + "h" : h ? h + "h " + m + "m" : m + "m";
  }

  function visible(tr) {
    var text = q && q.value.trim().toLowerCase();
    if (filter === "reject" && tr.dataset.ok === "1") return false;
    return !text || tr.dataset.search.indexOf(text) >= 0;
  }
  function applyFilter() {
    Array.prototype.forEach.call(authBody.rows, function (tr) { tr.hidden = !visible(tr); });
  }

  function addAuth(rows) {
    rows.slice().reverse().forEach(function (r) {        // oldest first, so the newest ends on top
      var tr = el("tr", "fresh");
      tr.dataset.ok = r.ok ? "1" : "0";
      tr.dataset.search = (r.user + " " + r.mac).toLowerCase();
      var t = td(r.time, "mono nowrap", "Time"); t.title = r.date; t.dataset.card = "key";
      var u = td("", "", "Username"); u.dataset.card = "title"; u.appendChild(el("b", "mono", r.user || "(empty)"));
      var res = td("", "", "Result"); res.dataset.card = "key"; res.appendChild(badge(r.ok ? "Accept" : "Reject", r.ok ? "ok" : "bad"));
      var why = td("", "", "Reason"); if (!r.ok) why.dataset.card = "key";      /* phones: a reason line only when refused */
      why.appendChild(r.ok ? el("span", "muted", "—") : el("span", "reason " + r.tone, r.reason));
      var nas = td(r.nas || "—", "nowrap", "Router"); nas.dataset.card = "key";
      var mac = td(r.mac || "—", "mono nowrap", "MAC"); if (!r.ok && r.mac) mac.dataset.card = "key";
      tr.append(t, u, res, why, nas, mac);
      tr.hidden = !visible(tr);
      authBody.insertBefore(tr, authBody.firstChild);
      setTimeout(function () { tr.classList.remove("fresh"); }, 1500);
    });
    while (authBody.rows.length > KEEP) authBody.deleteRow(-1);
    document.getElementById("noc-auth-empty").hidden = authBody.rows.length > 0;
  }

  function addAudit(rows) {
    rows.slice().reverse().forEach(function (r) {
      var tr = el("tr", "fresh");
      var t = td(r.time, "mono nowrap", "Time"); t.title = r.date + (r.ip ? " · " + r.ip : "");
      var w = td(r.who || "system", "", "Staff"); w.dataset.card = "title";
      var a = td(r.action, "", "Action"); a.dataset.card = "key";
      var g = td(r.target || "—", "mono", "Target"); g.dataset.card = "key"; if (r.detail) g.title = r.detail;
      tr.append(t, w, a, g);
      auditBody.insertBefore(tr, auditBody.firstChild);
      setTimeout(function () { tr.classList.remove("fresh"); }, 1500);
    });
    while (auditBody.rows.length > KEEP) auditBody.deleteRow(-1);
    document.getElementById("noc-audit-empty").hidden = auditBody.rows.length > 0;
  }

  function meter(label, value) {
    var row = el("div", "rmeter");
    row.append(el("span", "k", label));
    var bar = el("span", "meter" + (value === null ? "" : value > 85 ? " bad" : value > 70 ? " warn" : ""));
    var fill = el("i"); fill.style.width = (value === null ? 0 : Math.min(value, 100)) + "%";
    bar.appendChild(fill);
    row.append(bar, el("b", "num", value === null ? "—" : Math.round(value) + "%"));
    return row;
  }

  function renderRouters(list) {
    var box = document.getElementById("noc-routers");
    if (!list.length) {
      box.innerHTML = "";
      var e = el("div", "empty"); e.append(el("h3", "", "No routers yet"), el("p", "", "Add your MikroTik routers on the Routers page."));
      box.appendChild(e);
      return;
    }
    var frag = document.createDocumentFragment(), up = 0, down = 0;
    list.forEach(function (r) {
      if (r.state === "up") up++; else if (r.state === "down") down++;
      var card = el("article", "rcard " + r.state);
      var head = el("div", "rhead");
      var dot = el("span", "rdot " + r.state); dot.setAttribute("aria-hidden", "true");
      var name = el("div", "rname"); name.append(el("b", "", r.name), el("small", "mono", r.ip));
      var st = { up: ["Online", "ok"], down: ["Offline", "bad"], flaky: ["No reply", "warn"], unknown: ["Not checked", "mute"] }[r.state];
      head.append(dot, name, badge(st[0], st[1]));
      card.appendChild(head);
      card.append(meter("CPU", r.cpu), meter("RAM", r.ram));
      var facts = el("dl", "rfacts");
      [["PPPoE", r.pppoe.toLocaleString()], ["Uptime", dur(r.uptime)], ["Ping", r.rtt === null ? "—" : r.rtt.toFixed(1) + " ms"]].forEach(function (f) {
        var d = el("div"); d.append(el("dt", "", f[0]), el("dd", "num", f[1])); facts.appendChild(d);
      });
      card.appendChild(facts);
      if (r.board) card.appendChild(el("p", "rboard", r.board));
      if (r.error && r.state !== "up") card.appendChild(el("p", "rerr", r.error));
      else if (!r.snmp && r.state === "up") card.appendChild(el("p", "rboard", "Ping only: set an SNMP community for CPU, RAM and uptime."));
      frag.appendChild(card);
    });
    box.innerHTML = "";
    box.appendChild(frag);
    var sum = root.querySelector('[data-sum="routers"]'), sub = root.querySelector('[data-sum="routers-sub"]');
    sum.textContent = up + " / " + list.length;
    sub.textContent = down ? down + " offline" : "all answering";
    sub.className = "kcard-s" + (down ? " bad-text" : "");
  }

  function renderService(svc, time) {
    var pill = document.getElementById("noc-svc");
    pill.className = "svc-pill " + (svc.ok ? "ok" : "bad");
    pill.querySelector(".txt").textContent = svc.ok
      ? "NOC service running · last round " + svc.age + " s ago"
      : (svc.age === null ? "NOC service has not run yet (isp-panel-noc)" : "NOC service stopped " + Math.round(svc.age / 60) + " min ago");
    pill.title = "Updated " + time;
  }

  function tick() {
    if (paused || document.hidden || busy) return;
    busy = true;
    fetch(API + "?auth=" + lastAuth + "&audit=" + lastAudit, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { if (r.status === 403 || r.redirected) { paused = true; } return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        if (d.auth.length) { addAuth(d.auth); lastAuth = Math.max(lastAuth, d.auth[0].id); }
        if (d.audit.length) { addAudit(d.audit); lastAudit = Math.max(lastAudit, d.audit[0].id); }
        renderRouters(d.health);
        renderService(d.service, d.time);
        ["online", "accept", "reject"].forEach(function (k) { root.querySelector('[data-sum="' + k + '"]').textContent = d.summary[k].toLocaleString(); });
      })
      .catch(function () { /* offline for a moment: try again next tick */ })
      .then(function () { busy = false; });
  }

  document.getElementById("noc-pause").addEventListener("click", function () {
    paused = !paused;
    this.setAttribute("aria-pressed", paused ? "true" : "false");
    this.querySelector("span").textContent = paused ? "Resume" : "Pause";
    if (!paused) tick();
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-auth-filter]"), function (b, _, all) {
    b.addEventListener("click", function () {
      filter = b.dataset.authFilter;
      Array.prototype.forEach.call(document.querySelectorAll("[data-auth-filter]"), function (o) { o.setAttribute("aria-selected", o === b ? "true" : "false"); });
      applyFilter();
    });
  });
  if (q) q.addEventListener("input", applyFilter);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) tick(); });
  tick();
  timer = setInterval(tick, EVERY);
})();
