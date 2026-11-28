/* Table toolbar for any list (<div data-tt> ... <table data-tt-table>): rows per page, search, sort by a column,
   page numbers with "Showing 1 to 9 of 9 records", and Print / Copy / PDF / Excel / CSV / View (show or hide columns).

   Markup:
     <div data-tt="packages" data-tt-title="Packages">        the name keeps the reader's column choice per list
       <select data-tt-per>…</select>  <input data-tt-search>
       <button data-tt-do="print|copy|pdf|excel|csv">  <button data-tt-view> + <div data-tt-cols></div>
       <span data-tt-info></span> <nav data-tt-pages></nav>
       <table data-tt-table> <th data-k="name" data-sort="text|num"> … <td data-v="sort value"> …
   Cells with class "act" (buttons) are left out of every export. Everything happens in the browser (no CDN). */
(function () {
  function $$(sel, el) { return Array.prototype.slice.call((el || document).querySelectorAll(sel)); }
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }

  $$("[data-tt]").forEach(function (box) {
    var table = box.querySelector("[data-tt-table]"); if (!table) return;
    var key = "tt." + box.dataset.tt, title = box.dataset.ttTitle || document.title;
    var heads = $$("thead th", table), body = table.tBodies[0];
    var rows = $$("tr", body), per = 50, page = 1, sortCol = -1, sortDir = 1, query = "";
    var perSel = box.querySelector("[data-tt-per]"), search = box.querySelector("[data-tt-search]");
    var info = box.querySelector("[data-tt-info]"), pages = box.querySelector("[data-tt-pages]");
    var hidden = [], savedHidden = store(key + ".hidden");
    try { hidden = savedHidden ? JSON.parse(savedHidden)
                               : heads.filter(function (h) { return h.hasAttribute("data-tt-hide") && h.dataset.k; })
                                      .map(function (h) { return h.dataset.k; }); } catch (e) { hidden = []; }
    var savedPer = parseInt(store(key + ".per"), 10); if (savedPer) per = savedPer;
    if (box.hasAttribute("data-tt-server")) per = 1e9;      // the server pages and searches; only export / View here
    if (perSel) { perSel.value = String(per); if (perSel.value !== String(per)) { per = parseInt(perSel.value, 10) || 50; } }

    function cellText(td) { return (td.dataset.v !== undefined ? td.dataset.v : td.textContent).trim(); }
    function matches(r) { return !query || r.textContent.toLowerCase().indexOf(query) >= 0; }
    function visibleCols() { return heads.map(function (h, i) { return i; }).filter(function (i) {
      return heads[i].dataset.k ? hidden.indexOf(heads[i].dataset.k) < 0 : true; }); }

    function applyCols() {
      heads.forEach(function (h, i) {
        var off = h.dataset.k && hidden.indexOf(h.dataset.k) >= 0;
        h.classList.toggle("tt-off", !!off);
        rows.forEach(function (r) { if (r.cells[i]) r.cells[i].classList.toggle("tt-off", !!off); });
      });
    }
    function render() {
      var list = rows.filter(matches);
      if (sortCol >= 0) {
        var num = heads[sortCol].dataset.sort === "num";
        list.sort(function (a, b) {
          var x = cellText(a.cells[sortCol]), y = cellText(b.cells[sortCol]);
          if (num) { x = parseFloat(x) || 0; y = parseFloat(y) || 0; return (x - y) * sortDir; }
          return x.localeCompare(y, undefined, { numeric: true }) * sortDir;
        });
      }
      var total = list.length, pagesN = Math.max(1, Math.ceil(total / per));
      if (page > pagesN) page = pagesN;
      var from = (page - 1) * per;
      rows.forEach(function (r) { r.hidden = true; });
      list.forEach(function (r) { body.appendChild(r); });
      list.slice(from, from + per).forEach(function (r) { r.hidden = false; });
      if (info) info.textContent = total ? "Showing " + (from + 1) + " to " + Math.min(from + per, total) + " of " + total + " records"
                                         : "No matching records";
      if (pages) {
        pages.innerHTML = "";
        var btn = function (label, p, on, dis) {
          var b = document.createElement("button"); b.type = "button"; b.textContent = label; b.className = "tt-pg" + (on ? " on" : "");
          b.disabled = !!dis; if (on) b.setAttribute("aria-current", "page");
          b.addEventListener("click", function () { page = p; render(); }); pages.appendChild(b);
        };
        btn("‹", page - 1, false, page <= 1);
        for (var p = 1; p <= pagesN; p++) {
          if (pagesN > 7 && p !== 1 && p !== pagesN && Math.abs(p - page) > 1) {
            if (p === 2 || p === pagesN - 1) { var s = document.createElement("span"); s.className = "tt-gap"; s.textContent = "…"; pages.appendChild(s); }
            continue;
          }
          btn(String(p), p, p === page);
        }
        btn("›", page + 1, false, page >= pagesN);
      }
      heads.forEach(function (h, i) { if (h.dataset.sort) h.setAttribute("aria-sort", i === sortCol ? (sortDir > 0 ? "ascending" : "descending") : "none"); });
    }

    // rows of the current filter and sort (all pages) and the shown columns, for the exports
    function grid() {
      var cols = visibleCols().filter(function (i) { return !heads[i].classList.contains("act"); });
      var out = [cols.map(function (i) { return heads[i].textContent.trim(); })];
      rows.filter(matches).forEach(function (r) {
        out.push(cols.map(function (i) { return r.cells[i] ? (r.cells[i].dataset.x !== undefined ? r.cells[i].dataset.x : r.cells[i].textContent).replace(/\s+/g, " ").trim() : ""; }));
      });
      return out;
    }
    function download(name, text, type) {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([text], { type: type }));
      a.download = name; document.body.appendChild(a); a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 500);
    }
    function esc(v) { return String(v).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
    function htmlTable(g) {
      return "<table><thead><tr>" + g[0].map(function (h) { return "<th>" + esc(h) + "</th>"; }).join("") + "</tr></thead><tbody>" +
        g.slice(1).map(function (r) { return "<tr>" + r.map(function (c) { return "<td>" + esc(c) + "</td>"; }).join("") + "</tr>"; }).join("") +
        "</tbody></table>";
    }
    function printOut(asPdf) {
      var w = window.open("", "_blank"); if (!w) { window.print(); return; }
      w.document.write("<!doctype html><html><head><meta charset='utf-8'><title>" + esc(title) + "</title><style>" +
        "body{font:13px system-ui,sans-serif;margin:24px;color:#111}h1{font-size:18px;margin:0 0 4px}p{color:#666;margin:0 0 14px}" +
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:6px 8px;text-align:left}th{background:#f1f5f9}" +
        "</style></head><body><h1>" + esc(title) + "</h1><p>" + esc(new Date().toLocaleString()) +
        (asPdf ? " · choose “Save as PDF” as the printer" : "") + "</p>" + htmlTable(grid()) + "</body></html>");
      w.document.close(); w.focus(); setTimeout(function () { w.print(); }, 250);
    }
    var file = (box.dataset.tt || "table") + "-" + new Date().toISOString().slice(0, 10);
    $$("[data-tt-do]", box).forEach(function (b) {
      b.addEventListener("click", function () {
        var k = b.dataset.ttDo;
        if (k === "print") return printOut(false);
        if (k === "pdf") return printOut(true);
        if (k === "copy") {
          var txt = grid().map(function (r) { return r.join("\t"); }).join("\n");
          var done = function () { var o = b.textContent; b.textContent = "Copied"; setTimeout(function () { b.textContent = o; }, 1200); };
          if (navigator.clipboard) navigator.clipboard.writeText(txt).then(done, done); else done();
          return;
        }
        if (k === "csv") {
          var csv = grid().map(function (r) { return r.map(function (c) { c = /^[=+\-@]/.test(c) ? "'" + c : c; return /[",\n]/.test(c) ? '"' + c.replace(/"/g, '""') + '"' : c; }).join(","); }).join("\r\n");
          return download(file + ".csv", "﻿" + csv, "text/csv;charset=utf-8");
        }
        if (k === "excel") {
          return download(file + ".xls", "<html><head><meta charset='utf-8'></head><body>" + htmlTable(grid()) + "</body></html>",
                          "application/vnd.ms-excel");
        }
      });
    });

    // View: show / hide columns (remembered in this browser)
    var vbtn = box.querySelector("[data-tt-view]"), vpanel = box.querySelector("[data-tt-cols]");
    if (vbtn && vpanel) {
      heads.forEach(function (h) {
        if (!h.dataset.k) return;
        var l = document.createElement("label"); l.className = "check";
        var i = document.createElement("input"); i.type = "checkbox"; i.checked = hidden.indexOf(h.dataset.k) < 0;
        i.addEventListener("change", function () {
          hidden = hidden.filter(function (k) { return k !== h.dataset.k; }); if (!i.checked) hidden.push(h.dataset.k);
          store(key + ".hidden", JSON.stringify(hidden)); applyCols();
        });
        l.appendChild(i); l.appendChild(document.createTextNode(" " + h.textContent.trim())); vpanel.appendChild(l);
      });
      vbtn.addEventListener("click", function (e) { e.stopPropagation(); vpanel.hidden = !vpanel.hidden; vbtn.setAttribute("aria-expanded", String(!vpanel.hidden)); });
      document.addEventListener("click", function (e) { if (!vpanel.contains(e.target) && e.target !== vbtn) vpanel.hidden = true; });
    }

    heads.forEach(function (h, i) {
      if (!h.dataset.sort) return;
      h.tabIndex = 0; h.classList.add("tt-sort");
      var go = function () { sortDir = sortCol === i ? -sortDir : 1; sortCol = i; render(); };
      h.addEventListener("click", go);
      h.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    });
    if (perSel) perSel.addEventListener("change", function () { per = parseInt(perSel.value, 10) || 50; store(key + ".per", String(per)); page = 1; render(); });
    if (search) search.addEventListener("input", function () { query = search.value.trim().toLowerCase(); page = 1; render(); });
    applyCols(); render();
  });
})();
