/* Dependency-free SVG charts (works offline). Kinds: line, bars, donut.
   <div class="chart" data-kind="line" data-src="id"></div> + <script type="application/json" id="id">{...}</script> */
(function () {
  var NS = "http://www.w3.org/2000/svg";

  function svg(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function nice(max) {
    if (max <= 0) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(max))), f = max / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
  }
  function fmtNum(v) { return v >= 1000 ? (Math.round(v / 100) / 10) + "k" : String(Math.round(v)); }
  function fmtBytes(b) {
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return (i ? b.toFixed(b >= 100 ? 0 : 1) : Math.round(b)) + " " + u[i];
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  function frame(host, height) {
    host.innerHTML = "";
    var w = Math.max(host.clientWidth, 200);
    var s = svg("svg", { viewBox: "0 0 " + w + " " + height, height: height, role: "img" }, host);
    var tip = document.createElement("div");
    tip.className = "chart-tip";
    host.appendChild(tip);
    return { s: s, w: w, h: height, tip: tip };
  }
  function showTip(host, tip, x, y, html) {
    tip.innerHTML = html;
    tip.style.left = Math.min(Math.max(x, 60), host.clientWidth - 60) + "px";
    tip.style.top = y + "px";
    tip.classList.add("show");
  }

  function axes(f, pad, max, fmt) {
    var ticks = 4;
    for (var i = 0; i <= ticks; i++) {
      var v = max * i / ticks, y = pad.t + (f.h - pad.t - pad.b) * (1 - i / ticks);
      svg("line", { x1: pad.l, x2: f.w - pad.r, y1: y, y2: y, "class": "grid" }, f.s);
      var t = svg("text", { x: pad.l - 8, y: y + 4, "text-anchor": "end", "class": "ax" }, f.s);
      t.textContent = fmt(v);
    }
  }
  function xlabels(f, pad, labels, xAt) {
    var n = labels.length, step = Math.max(Math.ceil(n / Math.floor((f.w - pad.l - pad.r) / 70)), 1);
    for (var i = 0; i < n; i += step) {
      var t = svg("text", { x: xAt(i), y: f.h - 6, "text-anchor": "middle", "class": "ax" }, f.s);
      t.textContent = labels[i];
    }
  }

  function line(host, d) {
    var vals = d.values || [];
    if (vals.length < 2) {
      host.innerHTML = '<div class="chart-empty"><div>Collecting data.<br>The curve appears after the first few 5-minute samples.</div></div>';
      return;
    }
    var unit = d.unit, fmt = unit === "percent" ? function (v) { return (Math.round(v * 10) / 10) + "%"; }
      : unit === "load" ? function (v) { return (Math.round(v * 100) / 100).toString(); } : fmtNum;
    var f = frame(host, d.height || 190), pad = { l: unit ? 50 : 40, r: 10, t: 10, b: 24 };
    var max = nice(Math.max.apply(null, vals) * 1.08), color = css(d.color || "--fiber");
    var iw = f.w - pad.l - pad.r, ih = f.h - pad.t - pad.b;
    function X(i) { return pad.l + iw * i / (vals.length - 1); }
    function Y(v) { return pad.t + ih * (1 - v / max); }
    axes(f, pad, max, fmt);
    xlabels(f, pad, d.labels, X);
    var pts = vals.map(function (v, i) { return X(i).toFixed(1) + "," + Y(v).toFixed(1); });
    var area = svg("path", { d: "M" + X(0) + "," + Y(0) + " L" + pts.join(" L") + " L" + X(vals.length - 1) + "," + Y(0) + " Z", fill: color, "fill-opacity": ".13" }, f.s);
    var ln = svg("path", { d: "M" + pts.join(" L"), "class": "line", stroke: color }, f.s);
    if (!host.dataset.drawn) {
      host.dataset.drawn = "1";
      var len = Math.ceil(ln.getTotalLength ? ln.getTotalLength() : 2000);
      ln.style.setProperty("--len", len);
      ln.setAttribute("class", "line draw");
    }
    var last = svg("circle", { cx: X(vals.length - 1), cy: Y(vals[vals.length - 1]), r: 4, fill: color, stroke: css("--surface"), "stroke-width": 2 }, f.s);
    var guide = svg("line", { y1: pad.t, y2: pad.t + ih, "class": "guide", opacity: 0 }, f.s);
    var dot = svg("circle", { r: 4.5, fill: color, stroke: css("--surface"), "stroke-width": 2, opacity: 0 }, f.s);
    f.s.addEventListener("pointermove", function (e) {
      var r = f.s.getBoundingClientRect(), x = (e.clientX - r.left) * f.w / r.width;
      var i = Math.round((x - pad.l) / iw * (vals.length - 1));
      i = Math.min(Math.max(i, 0), vals.length - 1);
      guide.setAttribute("x1", X(i)); guide.setAttribute("x2", X(i)); guide.setAttribute("opacity", 1);
      dot.setAttribute("cx", X(i)); dot.setAttribute("cy", Y(vals[i])); dot.setAttribute("opacity", 1);
      showTip(host, f.tip, X(i) * r.width / f.w, Y(vals[i]) , "<b>" + (unit ? fmt(vals[i]) : vals[i].toLocaleString()) + "</b>" + (unit ? (d.suffix || "") : " online") + "<br>" + esc((d.stamps || d.labels)[i]));
    });
    f.s.addEventListener("pointerleave", function () { guide.setAttribute("opacity", 0); dot.setAttribute("opacity", 0); f.tip.classList.remove("show"); });
    void area; void last;
  }

  function fmtBps(v) {
    var u = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"], i = 0;
    while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    return (i ? v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2) : Math.round(v)) + " " + u[i];
  }

  /* Several series as soft areas with lines on top (download vs upload). d.series[k].color is a CSS variable name. */
  function area(host, d) {
    var series = d.series || [], labels = d.labels || [], n = labels.length;
    if (n < 2) {
      host.innerHTML = '<div class="chart-empty"><div>Collecting data.<br>Throughput is sampled every 5 minutes; the chart fills in as samples arrive.</div></div>';
      return;
    }
    var fmt = d.unit === "bps" ? fmtBps : fmtNum;
    var f = frame(host, d.height || 240), pad = { l: 70, r: 12, t: 12, b: 24 };
    var peak = 0;
    series.forEach(function (s) { s.values.forEach(function (v) { if (v > peak) peak = v; }); });
    var max = nice(peak * 1.1 || 1), iw = f.w - pad.l - pad.r, ih = f.h - pad.t - pad.b;
    function X(i) { return pad.l + iw * i / (n - 1); }
    function Y(v) { return pad.t + ih * (1 - v / max); }
    axes(f, pad, max, fmt);
    xlabels(f, pad, labels, X);
    var colors = series.map(function (s, k) { return css(s.color || ["--c1", "--c2", "--c3"][k % 3]); });
    series.forEach(function (s, k) {
      var pts = s.values.map(function (v, i) { return X(i).toFixed(1) + "," + Y(v).toFixed(1); });
      svg("path", { d: "M" + X(0) + "," + Y(0) + " L" + pts.join(" L") + " L" + X(n - 1) + "," + Y(0) + " Z", fill: colors[k], "fill-opacity": k ? ".10" : ".16" }, f.s);
      svg("path", { d: "M" + pts.join(" L"), "class": "line", stroke: colors[k] }, f.s);
    });
    var guide = svg("line", { y1: pad.t, y2: pad.t + ih, "class": "guide", opacity: 0 }, f.s);
    var dots = series.map(function (s, k) { return svg("circle", { r: 4, fill: colors[k], stroke: css("--surface"), "stroke-width": 2, opacity: 0 }, f.s); });
    f.s.addEventListener("pointermove", function (e) {
      var r = f.s.getBoundingClientRect(), x = (e.clientX - r.left) * f.w / r.width;
      var i = Math.min(Math.max(Math.round((x - pad.l) / iw * (n - 1)), 0), n - 1);
      guide.setAttribute("x1", X(i)); guide.setAttribute("x2", X(i)); guide.setAttribute("opacity", 1);
      var html = "<b>" + esc((d.stamps || labels)[i]) + "</b>";
      series.forEach(function (s, k) {
        dots[k].setAttribute("cx", X(i)); dots[k].setAttribute("cy", Y(s.values[i])); dots[k].setAttribute("opacity", 1);
        html += '<br><i class="sw" style="background:' + colors[k] + '"></i>' + esc(s.name) + ": " + fmt(s.values[i]);
      });
      showTip(host, f.tip, X(i) * r.width / f.w, Y(Math.max.apply(null, series.map(function (s) { return s.values[i]; }))), html);
    });
    f.s.addEventListener("pointerleave", function () {
      guide.setAttribute("opacity", 0); dots.forEach(function (c) { c.setAttribute("opacity", 0); }); f.tip.classList.remove("show");
    });
    var lg = document.createElement("div");
    lg.className = "legend";
    lg.innerHTML = series.map(function (s, k) {
      var last = s.values[n - 1];
      return '<span><i style="background:' + colors[k] + '"></i>' + esc(s.name) + ' <b class="num">' + fmt(last) + "</b></span>";
    }).join("");
    host.appendChild(lg);
  }

  function fmtMoney(v) { return v >= 1e6 ? (Math.round(v / 1e5) / 10) + "M" : v >= 1000 ? (Math.round(v / 100) / 10) + "k" : String(Math.round(v)); }

  function bars(host, d) {
    var fmt = d.unit === "money" ? fmtMoney : d.unit === "number" ? fmtNum : fmtBytes;
    var tipFmt = d.unit === "money" || d.unit === "number" ? function (v) { return Math.round(v).toLocaleString(); } : fmtBytes;
    var series = d.series || [], n = (d.labels || []).length;
    var totals = [], i, j;
    for (i = 0; i < n; i++) { var t = 0; series.forEach(function (s) { t += s.values[i] || 0; }); totals.push(t); }
    if (!n || Math.max.apply(null, totals) === 0) {
      host.innerHTML = '<div class="chart-empty"><div>Nothing recorded in this period.</div></div>';
      return;
    }
    var f = frame(host, d.height || 220), pad = { l: 56, r: 8, t: 10, b: 24 };
    var max = nice(Math.max.apply(null, totals) * 1.05), colors = (d.colors || ["--c1", "--c2", "--c3", "--c4"]).map(css);
    var iw = f.w - pad.l - pad.r, ih = f.h - pad.t - pad.b, slot = iw / n, bw = Math.max(Math.min(slot * 0.68, 26), 2);
    axes(f, pad, max, fmt);
    xlabels(f, pad, d.labels, function (k) { return pad.l + slot * (k + 0.5); });
    for (i = 0; i < n; i++) {
      var y0 = pad.t + ih, x = pad.l + slot * (i + 0.5) - bw / 2;
      for (j = 0; j < series.length; j++) {
        var h = ih * (series[j].values[i] || 0) / max;
        if (h > 0) { svg("rect", { x: x, y: y0 - h, width: bw, height: h, fill: colors[j], rx: 1.5 }, f.s); y0 -= h; }
      }
      (function (idx) {
        var hit = svg("rect", { x: pad.l + slot * idx, y: pad.t, width: slot, height: ih, fill: "transparent" }, f.s);
        hit.addEventListener("pointermove", function () {
          var html = "<b>" + esc(d.labels[idx]) + "</b>";
          series.forEach(function (s) { html += "<br>" + esc(s.name) + ": " + tipFmt(s.values[idx] || 0); });
          var r = f.s.getBoundingClientRect();
          showTip(host, f.tip, (pad.l + slot * (idx + 0.5)) * r.width / f.w, pad.t + ih * (1 - totals[idx] / max), html);
        });
        hit.addEventListener("pointerleave", function () { f.tip.classList.remove("show"); });
      })(i);
    }
    var lg = document.createElement("div");
    lg.className = "legend";
    lg.innerHTML = series.map(function (s, k) { return '<span><i style="background:' + colors[k] + '"></i>' + esc(s.name) + "</span>"; }).join("");
    host.appendChild(lg);
  }

  function donut(host, d) {
    var items = (d.items || []).filter(function (x) { return x.value > 0; });
    host.innerHTML = "";
    if (!items.length) { host.innerHTML = '<div class="chart-empty"><div>No subscribers yet.</div></div>'; return; }
    var total = items.reduce(function (a, b) { return a + b.value; }, 0);
    var wrap = document.createElement("div");
    wrap.className = "donut-wrap";
    var box = document.createElement("div");
    wrap.appendChild(box);
    var s = svg("svg", { viewBox: "0 0 150 150", width: 150, height: 150, role: "img" }, box);
    var R = 56, C = 2 * Math.PI * R, off = 0, palette = ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6"];
    svg("circle", { cx: 75, cy: 75, r: R, fill: "none", stroke: css("--surface-2"), "stroke-width": 20 }, s);
    var ul = document.createElement("ul");
    var link = host.dataset.link;
    items.forEach(function (it, k) {
      var color = css(it.color || palette[Math.min(k, 5)]), len = C * it.value / total;
      svg("circle", { cx: 75, cy: 75, r: R, fill: "none", stroke: color, "stroke-width": 20, "stroke-dasharray": Math.max(len - 1.5, 0.5) + " " + C, "stroke-dashoffset": -off, transform: "rotate(-90 75 75)" }, s);
      off += len;
      var li = document.createElement("li"), pct = Math.round(1000 * it.value / total) / 10;
      var body = '<i style="background:' + color + '"></i><span>' + esc(it.name) + '</span><span class="num">' + it.value.toLocaleString() + ' <small class="muted">' + pct + "%</small></span>";
      li.innerHTML = (link && it.status) ? '<a href="' + esc(link + "?status=" + encodeURIComponent(it.status)) + '">' + body + "</a>" : body;
      ul.appendChild(li);
    });
    var t1 = svg("text", { x: 75, y: 78, "text-anchor": "middle", "class": "donut-center" }, s); t1.textContent = total.toLocaleString();
    var t2 = svg("text", { x: 75, y: 94, "text-anchor": "middle", "class": "donut-sub" }, s); t2.textContent = d.center || "subscribers";
    wrap.appendChild(ul);
    host.appendChild(wrap);
  }

  var kinds = { line: line, bars: bars, donut: donut, area: area };
  function drawOne(host) {
    var src = document.getElementById(host.dataset.src), data = {};
    try { data = JSON.parse(src.textContent); } catch (e) { /* ignore */ }
    if (kinds[host.dataset.kind]) kinds[host.dataset.kind](host, data);
  }
  function drawAll() { document.querySelectorAll(".chart[data-kind]").forEach(drawOne); }
  var timer;
  function redraw() { clearTimeout(timer); timer = setTimeout(drawAll, 80); }
  window.addEventListener("resize", redraw);
  document.addEventListener("themechange", drawAll);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", drawAll); else drawAll();
  window.IspCharts = { redraw: drawAll, draw: drawOne, fmtBps: fmtBps };
})();
