/* Live RX / TX chart on a subscriber's page.
   Polls /subscribers/<id>/rate every 30 s while the page is visible and draws the last 30 minutes it has seen.
   Nothing is stored: the chart starts empty each time the page opens. Routers send new byte counters only at each
   interim update (5 minutes on MikroTik by default), so the lines move in steps. */
(function () {
  var box = document.querySelector(".live-rate[data-rate-src]");
  if (!box) return;
  var src = box.getAttribute("data-rate-src"), MAX = 60, EVERY = 30000, W = 600, H = 150, PAD = 6;
  var pts = [], timer = null;
  function q(s) { return box.querySelector(s); }
  function bps(v) {
    v = Number(v) || 0;
    var u = ["bps", "Kbps", "Mbps", "Gbps"];
    for (var i = 0; i < u.length; i++) {
      if (v < 1000 || i === u.length - 1) return i === 0 ? Math.round(v) + " bps" : (v < 100 ? v.toFixed(2) : v.toFixed(0)) + " " + u[i];
      v /= 1000;
    }
  }
  function draw() {
    var top = 0;
    pts.forEach(function (p) { top = Math.max(top, p.d, p.u); });
    top = top > 0 ? top * 1.15 : 1;
    function line(key) {
      if (!pts.length) return "";
      var step = (W - 2 * PAD) / (MAX - 1), start = W - PAD - (pts.length - 1) * step;
      return pts.map(function (p, i) {
        return (start + i * step).toFixed(1) + "," + (H - PAD - (p[key] / top) * (H - 2 * PAD)).toFixed(1);
      }).join(" ");
    }
    q("[data-lr-rx]").setAttribute("points", line("d"));
    q("[data-lr-tx]").setAttribute("points", line("u"));
    var g = q("[data-lr-grid]"), html = "";
    for (var i = 1; i <= 3; i++) { var y = (PAD + (H - 2 * PAD) * i / 4).toFixed(1); html += '<line x1="0" x2="' + W + '" y1="' + y + '" y2="' + y + '"></line>'; }
    g.innerHTML = html;
    q("[data-lr-max]").textContent = pts.length ? "top " + bps(top) : "";
  }
  function show(d) {
    var note = q("[data-lr-note]");
    if (!d.online) {
      q("[data-lr-down]").textContent = "Offline"; q("[data-lr-up]").textContent = "Offline";
      note.textContent = "Not connected right now";
      pts.push({ d: 0, u: 0 });
    } else if (!d.known) {
      q("[data-lr-down]").textContent = "–"; q("[data-lr-up]").textContent = "–";
      note.textContent = "Online · waiting for the first router update";
    } else {
      q("[data-lr-down]").textContent = bps(d.down); q("[data-lr-up]").textContent = bps(d.up);
      note.textContent = "Online · router update " + (d.updated || "") + " (about every 5 min)";
      pts.push({ d: d.down, u: d.up });
    }
    if (pts.length > MAX) pts = pts.slice(pts.length - MAX);
    draw();
  }
  function poll() {
    if (document.hidden) return;
    fetch(src, { credentials: "same-origin", headers: { Accept: "application/json" }, cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) show(d); })
      .catch(function () { q("[data-lr-note]").textContent = "Could not reach the panel; retrying"; });
  }
  poll();
  timer = setInterval(poll, EVERY);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });
})();
