/* ISP RADIUS Panel - small UI behaviours, no dependencies. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }

  /* theme (topbar button and sidebar button both work) */
  $$("[data-theme-toggle]").forEach(function (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      store("isp-theme", next);
      document.dispatchEvent(new Event("themechange"));
    });
  });

  /* desktop: hide the menu for a full-width page (remembered) */
  $$("[data-side-toggle]").forEach(function (b) {
    b.addEventListener("click", function () {
      var off = document.documentElement.classList.toggle("side-off");
      try { localStorage.setItem("isp-side", off ? "off" : "on"); } catch (e) {}
    });
  });
  /* mobile sidebar */
  $$("[data-nav-toggle]").forEach(function (b) { b.addEventListener("click", function () { document.body.classList.toggle("nav-open"); }); });
  document.addEventListener("click", function (e) {
    if (document.body.classList.contains("nav-open") && !e.target.closest(".side") && !e.target.closest("[data-nav-toggle]")) document.body.classList.remove("nav-open");
  });

  /* toasts */
  $$(".toast").forEach(function (t) {
    var close = $("button", t);
    if (close) close.addEventListener("click", function () { t.remove(); });
    if (!t.classList.contains("error")) setTimeout(function () { t.remove(); }, 7000);
  });

  /* dialogs */
  $$("[data-open]").forEach(function (b) {
    b.addEventListener("click", function () {
      var dlg = document.getElementById(b.dataset.open);
      if (!dlg) return;
      var form = $("form", dlg);
      if (b.hasAttribute("data-new") && form) {
        form.reset();
        $$("[name=id]", form).forEach(function (i) { i.value = ""; });
        $$("[data-new-hide]", dlg).forEach(function (n) { n.classList.add("hide"); });
        $$("[data-new-only]", dlg).forEach(function (n) { n.classList.remove("hide"); });
        $$("[data-title]", dlg).forEach(function (n) { n.textContent = n.dataset.titleNew || n.textContent; });
      }
      if (b.dataset.edit && form) {
        var data = JSON.parse(b.dataset.edit);
        form.reset();
        Object.keys(data).forEach(function (k) {
          var f = form.elements[k];
          if (!f || data[k] === null || f.type === "file") return;
          if (f.multiple) { var want = [].concat(data[k]).map(String); $$("option", f).forEach(function (o) { o.selected = want.indexOf(o.value) >= 0; }); }
          else if (f.type === "checkbox") f.checked = !!data[k] && data[k] !== "0";
          else f.value = data[k];
        });
        $$("[data-new-hide]", dlg).forEach(function (n) { n.classList.remove("hide"); });
        $$("[data-new-only]", dlg).forEach(function (n) { n.classList.add("hide"); });
        $$("[data-title]", dlg).forEach(function (n) { n.textContent = n.dataset.titleEdit || n.textContent; });
        $$("[data-lock-on-edit]", dlg).forEach(function (n) { n.readOnly = true; });
      } else {
        $$("[data-lock-on-edit]", dlg).forEach(function (n) { n.readOnly = false; });
      }
      if (b.dataset.fill && form) {
        var fill = JSON.parse(b.dataset.fill);
        Object.keys(fill).forEach(function (k) { if (form.elements[k]) form.elements[k].value = fill[k]; });
      }
      /* one drawer serves every row: the button gives the form its address and the heading its name */
      if (b.dataset.action && form) form.action = b.dataset.action;
      if (b.dataset.label) $$("[data-label-slot]", dlg).forEach(function (n) { n.textContent = b.dataset.label; });
      if (form) form.dispatchEvent(new Event("filled"));
      dlg.showModal();
      var first = $("input:not([type=hidden]):not([readonly]), select", dlg);
      if (first) first.focus();
      dlg.dispatchEvent(new Event("opened"));
    });
  });
  $$("dialog").forEach(function (d) {
    d.addEventListener("click", function (e) { if (e.target === d) d.close(); });
    $$("[data-close]", d).forEach(function (b) { b.addEventListener("click", function () { d.close(); }); });
  });

  /* confirm before destructive actions: styled dialog, native confirm only as fallback */
  var confirmDlg = document.getElementById("confirm-dlg"), pendingSubmit = null;
  function openConfirm(msg, alertOnly) {
    if (!confirmDlg) return false;
    $("[data-confirm-msg]", confirmDlg).textContent = msg;
    var cancel = $("[data-confirm-cancel]", confirmDlg), yes = $("[data-confirm-yes]", confirmDlg);
    if (cancel) cancel.hidden = !!alertOnly;
    if (yes) yes.textContent = alertOnly ? "OK" : "Confirm";
    confirmDlg.showModal();
    return true;
  }
  window.panelAlert = function (msg) { pendingSubmit = null; if (!openConfirm(msg, true)) window.alert(msg); };
  /* Cancel, the X and Esc all close the dialog: forget the form, so a later "OK" can never submit it */
  if (confirmDlg) confirmDlg.addEventListener("close", function () { pendingSubmit = null; });
  if (confirmDlg) $("[data-confirm-yes]", confirmDlg).addEventListener("click", function () {
    var p = pendingSubmit;
    confirmDlg.close();
    if (p) {
      p.form.dataset.confirmed = "1";
      if (p.form.requestSubmit) p.form.requestSubmit(p.submitter || undefined); else p.form.submit();
    }
  });
  document.addEventListener("submit", function (e) {
    var msg = e.target.dataset.confirm || (e.submitter && e.submitter.dataset.confirm);
    if (!msg || e.target.dataset.confirmed) { delete e.target.dataset.confirmed; return; }
    e.preventDefault();
    var opened = openConfirm(msg, false);
    if (opened) pendingSubmit = { form: e.target, submitter: e.submitter };
    else if (window.confirm(msg)) {
      e.target.dataset.confirmed = "1";
      if (e.target.requestSubmit) e.target.requestSubmit(e.submitter || undefined); else e.target.submit();
    }
  });

  /* busy state: stop double-submits (Pay, Renew, Activate...) */
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;
    var b = e.submitter;
    if (!b || b.disabled) return;
    b.classList.add("busy"); b.setAttribute("aria-busy", "true");
    /* disable only after the browser has read the form: a button disabled during "submit" drops its own
       name=value (e.g. "Remove photo" would quietly become "Save"); .busy already blocks a second click */
    setTimeout(function () { b.disabled = true; }, 0);
    setTimeout(function () { b.classList.remove("busy"); b.disabled = false; b.removeAttribute("aria-busy"); }, 4000);
  });
  window.addEventListener("pageshow", function () {
    $$("button.busy").forEach(function (b) { b.classList.remove("busy"); b.disabled = false; b.removeAttribute("aria-busy"); });
  });

  /* bulk selection */
  var bulk = $("[data-bulk]");
  if (bulk) {
    var master = $("[data-check-all]", bulk), bar = $("[data-bulkbar]"), counter = $("[data-selected]");
    var boxes = function () { return $$(".row-check", bulk); };
    var refresh = function () {
      var n = boxes().filter(function (b) { return b.checked; }).length;
      if (bar) bar.classList.toggle("show", n > 0);
      if (counter) counter.textContent = n;
      if (master) { master.checked = n > 0 && n === boxes().length; master.indeterminate = n > 0 && n < boxes().length; }
    };
    if (master) master.addEventListener("change", function () { boxes().forEach(function (b) { b.checked = master.checked; }); refresh(); });
    bulk.addEventListener("change", function (e) { if (e.target.classList.contains("row-check")) refresh(); });
    var action = $("[data-bulk-action]");
    if (action) {
      var sync = function () {
        $$("[data-for]", bar).forEach(function (n) { n.classList.toggle("hide", n.dataset["for"].split(" ").indexOf(action.value) < 0); });
      };
      action.addEventListener("change", sync); sync();
    }
  }

  /* reveal / copy / generate */
  $$("[data-reveal]").forEach(function (b) {
    b.addEventListener("click", function () {
      var t = document.getElementById(b.dataset.reveal);
      var shown = t.dataset.shown === "1";
      t.textContent = shown ? "••••••••" : t.dataset.value;
      t.dataset.shown = shown ? "0" : "1";
      b.setAttribute("aria-pressed", shown ? "false" : "true");
    });
  });
  $$("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var el = document.getElementById(b.dataset.copy);
      var text = el.dataset.value || el.value || el.textContent;
      var done = function () { var o = b.innerHTML; b.textContent = "Copied"; setTimeout(function () { b.innerHTML = o; }, 1200); };
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(done); else { done(); }
    });
  });
  $$("[data-gen]").forEach(function (b) {
    b.addEventListener("click", function () {
      var chars = "abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRTUVWXY2346789", out = "", buf = new Uint32Array(10);
      crypto.getRandomValues(buf);
      for (var i = 0; i < 10; i++) out += chars[buf[i] % chars.length];
      var t = document.getElementById(b.dataset.gen); t.value = out; t.type = "text";
    });
  });

  /* expiry quick buttons */
  $$("[data-add-days]").forEach(function (b) {
    b.addEventListener("click", function () {
      var t = document.getElementById(b.dataset.target), n = b.dataset.addDays;
      if (n === "none") { t.value = ""; return; }
      var base = new Date(); base.setHours(12, 0, 0, 0);
      if (t.value) { var cur = new Date(t.value + "T12:00:00"); if (cur > base) base = cur; }
      base.setDate(base.getDate() + parseInt(n, 10));
      t.value = base.toISOString().slice(0, 10);
    });
  });

  /* plan form: live rate string */
  var plan = $("#plan-form");
  if (plan) {
    var out = $("[data-rate-preview]", plan);
    var upd = function () {
      var custom = plan.elements.rate_custom.value.trim();
      if (out) out.textContent = custom || "?";
    };
    plan.addEventListener("input", upd);
    var pdlg = plan.closest("dialog");
    if (pdlg) pdlg.addEventListener("opened", upd); else upd();
    /* a switch enables the field(s) that depend on it */
    $$("[data-switch]", plan).forEach(function (sw) {
      var deps = $$("[data-dep=" + sw.dataset["switch"] + "]", plan);
      var apply = function () { deps.forEach(function (d) { d.disabled = !sw.checked; }); };
      sw.addEventListener("change", function () { apply(); var l = sw.nextElementSibling; if (l) l.textContent = sw.checked ? "On" : "Off"; }); apply();
    });
    /* quota "switch to policy" shows the policy picker */
    $$("[data-action-for]", plan).forEach(function (sel) {
      var tgt = $("[data-policy-for=" + sel.dataset.actionFor + "]", plan);
      var apply = function () { tgt.disabled = sel.value !== "policy"; };
      sel.addEventListener("change", apply); apply();
    });
  }

  /* auto refresh (live lists) */
  var auto = $("[data-autorefresh]");
  if (auto) {
    var secs = parseInt(auto.dataset.interval || "30", 10) || 30;
    auto.checked = (store("isp-auto") || (auto.dataset.interval ? "1" : "0")) === "1";   // the Online page starts switched on; the choice is remembered
    var timer;
    var arm = function () {
      clearInterval(timer);
      if (auto.checked) timer = setInterval(function () { if (!$("dialog[open]") && !document.hidden) location.reload(); }, secs * 1000);
    };
    auto.addEventListener("change", function () { store("isp-auto", auto.checked ? "1" : "0"); arm(); });
    arm();
  }

  /* dashboard live numbers */
  var live = $$("[data-live]");
  if (live.length) {
    setInterval(function () {
      if (document.hidden) return;
      fetch("/api/live", { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) return;
          live.forEach(function (n) { var v = d[n.dataset.live]; if (v !== undefined) n.textContent = (typeof v === "number") ? v.toLocaleString() : v; });
        }).catch(function () { /* offline: keep old numbers */ });
    }, 30000);
  }

  /* sidebar groups
     Desktop with the menu collapsed to icons (the << button, or a tablet-width screen): a group's submenu opens when
     the mouse is over it and stays while the mouse is on it; 5 s after the mouse leaves it closes again. Pages open
     with every group closed (the current group's icon stays highlighted).
     Desktop with the full menu shown: as before, groups open and close by click and the current group stays open.
     Phone (menu drawer): the menu stays open while it is being used and hides by itself 5 s after the last touch,
     scroll or key press in it. Picking an item opens that page, which starts with the menu hidden. */
  var MENU_WAIT = 5000;
  var hoverMenu = window.matchMedia && matchMedia("(hover: hover) and (pointer: fine) and (min-width: 768px)");
  var railWidth = window.matchMedia && matchMedia("(max-width: 1023px)");
  var wideScreen = window.matchMedia && matchMedia("(min-width: 768px)");
  function rail() {             /* the menu shows icons only and groups open as flyouts (mouse or touch) */
    return !!(wideScreen && wideScreen.matches) &&
      (document.documentElement.classList.contains("side-off") || !!(railWidth && railWidth.matches));
  }
  function desktop() {          /* hover mode: a mouse, and the menu shows icons only */
    return !!(hoverMenu && hoverMenu.matches) && rail();
  }
  var groups = $$(".nav-group");
  groups.forEach(function (d) { d.dataset.wasOpen = d.open ? "1" : ""; });   /* the server's choice (current group) */
  function closeOthers(d) { groups.forEach(function (o) { if (o !== d && o.open) o.open = false; }); }
  function placeSub(d) {        /* a flyout of a low group would run off the bottom of the screen: lift it up */
    var sub = $(".nav-sub", d);
    if (!sub) return;
    sub.style.top = "";
    if (getComputedStyle(sub).position !== "absolute" || getComputedStyle(sub).display === "none") return;
    var r = sub.getBoundingClientRect(), over = r.bottom - (window.innerHeight - 8);
    if (over > 0) sub.style.top = -Math.min(over, Math.max(r.top - 8, 0)) + "px";
  }
  function applyMode() {        /* icons-only menu: all closed (a flyout would cover the page); full menu: current group open */
    document.documentElement.classList.toggle("hover-menu", desktop());
    var closed = rail();
    groups.forEach(function (d) { d.open = closed ? false : d.dataset.wasOpen === "1"; });
  }
  applyMode();
  document.documentElement.classList.add("menu-ready");
  $$("[data-side-toggle]").forEach(function (b) { b.addEventListener("click", function () { setTimeout(applyMode, 0); }); });
  if (railWidth && railWidth.addEventListener) railWidth.addEventListener("change", applyMode);
  if (wideScreen && wideScreen.addEventListener) wideScreen.addEventListener("change", applyMode);
  /* icons-only menu on a touch screen (a phone in desktop view): a tap anywhere outside the open flyout closes it */
  document.addEventListener("pointerdown", function (e) {
    if (!rail() || desktop()) return;
    var inGroup = e.target.closest && e.target.closest(".nav-group");
    groups.forEach(function (o) { if (o.open && o !== inGroup) o.open = false; });
  });
  groups.forEach(function (d) {
    var timer = null, intent = null;
    function stop() { if (timer) { clearTimeout(timer); timer = null; } }
    d.addEventListener("mouseenter", function () {
      if (!desktop()) return;
      stop();
      placeSub(d);
      /* a short pause first, so sliding the mouse past a group on the way to another does not open it */
      intent = setTimeout(function () { intent = null; closeOthers(d); d.open = true; }, 150);
    });
    d.addEventListener("mouseleave", function () {
      if (!desktop()) return;
      if (intent) { clearTimeout(intent); intent = null; }
      stop(); timer = setTimeout(function () { timer = null; d.open = false; }, MENU_WAIT);
    });
    d.addEventListener("toggle", function () {
      if (!d.open) { stop(); return; }
      placeSub(d);
      if (rail()) closeOthers(d);          /* icons only: one flyout at a time, mouse or touch */
      else groups.forEach(function (o) { if (o !== d && o.open && !$(".on", o)) o.open = false; });
    });
  });
  var navTimer = null;
  function navIdle() {
    if (navTimer) clearTimeout(navTimer);
    navTimer = document.body.classList.contains("nav-open")
      ? setTimeout(function () { navTimer = null; document.body.classList.toggle("nav-open", false); }, MENU_WAIT) : null;
  }
  $$("[data-nav-toggle]").forEach(function (b) { b.addEventListener("click", function () { setTimeout(navIdle, 0); }); });
  var side = $(".side");
  if (side) {
    var idleAt = 0;                    /* scroll fires every frame: restart the 5 s wait at most every 200 ms */
    ["touchstart", "pointerdown", "scroll", "wheel", "keydown", "toggle"].forEach(function (ev) {
      side.addEventListener(ev, function () {
        if (!document.body.classList.contains("nav-open")) return;
        var now = Date.now();
        if (ev === "scroll" && now - idleAt < 200) return;
        idleAt = now;
        navIdle();
      }, { passive: true, capture: true });
    });
    side.addEventListener("click", function (e) {      /* an item was picked: keep the menu until the next page shows */
      if (e.target.closest(".nav a[href]") && navTimer) { clearTimeout(navTimer); navTimer = null; }
    });
  }
  window.addEventListener("pageshow", function (e) {    /* back button from the bfcache: start with the menu hidden */
    if (!e.persisted) return;
    document.body.classList.toggle("nav-open", false);
    if (desktop()) groups.forEach(function (d) { d.open = false; });
    else groups.forEach(function (d) { d.open = d.dataset.wasOpen === "1"; });
  });

  /* compose pages (SMS / email) */
  var comp = $("#compose-form");
  if (comp) {
    var body = comp.elements.body, subject = comp.elements.subject, count = $("[data-sms-count]", comp);
    var gsm = /^[\n\r\x20-\x7e£¥èéùìòÇØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ¤¡ÄÖÑÜ§¿äöñüà]*$/;
    var tally = function () {
      if (!count) return;
      var t = body.value, uni = !gsm.test(t), per = uni ? 70 : 160, multi = uni ? 67 : 153;
      var parts = t.length <= per ? (t.length ? 1 : 0) : Math.ceil(t.length / multi);
      count.textContent = t.length + " characters · " + parts + " SMS" + (uni ? " (Unicode)" : "");
    };
    body.addEventListener("input", tally); tally();
    var picker = $("[data-template]", comp);
    if (picker) picker.addEventListener("change", function () {
      var o = picker.options[picker.selectedIndex];
      if (!o.value) return;
      body.value = o.dataset.body || ""; if (subject) subject.value = o.dataset.subject || ""; tally();
    });
    $$("[data-var]", comp).forEach(function (b) {
      b.addEventListener("click", function () {
        var tgt = document.activeElement === subject ? subject : body, a = tgt.selectionStart || tgt.value.length, e = tgt.selectionEnd || a;
        tgt.value = tgt.value.slice(0, a) + "{" + b.dataset.var + "}" + tgt.value.slice(e);
        tgt.focus(); tally();
      });
    });
    var aud = $$("[name=audience]", comp), box = $("[data-audience-count]", comp);
    var showAud = function () {
      var v = (aud.filter(function (r) { return r.checked; })[0] || {}).value;
      $$("[data-aud]", comp).forEach(function (n) { n.classList.toggle("hide", n.dataset.aud !== v); });
      if (v === "filter") recount();
    };
    var recount = function () {
      if (!box) return;
      var p = new URLSearchParams({ channel: comp.dataset.channel });
      ["plan", "area", "status"].forEach(function (k) { if (comp.elements[k] && comp.elements[k].value) p.set(k, comp.elements[k].value); });
      fetch("/api/recipients?" + p, { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d) box.textContent = d.count.toLocaleString() + " recipient" + (d.count === 1 ? "" : "s"); });
    };
    aud.forEach(function (r) { r.addEventListener("change", showAud); });
    ["plan", "area", "status"].forEach(function (k) { if (comp.elements[k]) comp.elements[k].addEventListener("change", recount); });
    showAud();
  }

  /* print buttons */
  $$("[data-print]").forEach(function (b) { b.addEventListener("click", function () { window.print(); }); });

  /* Policy editor: repeatable attribute rows */
  (function () {
    var dlg = $("#policy-dlg"); if (!dlg) return;
    var rows = $("#policy-rows"), tpl = $("#policy-row-tpl"), form = $("form", dlg);
    var addRow = function (d) {
      var n = tpl.content.firstElementChild.cloneNode(true);
      if (d) { Object.keys(d).forEach(function (k) { var f = $("[name=" + k + "]", n); if (f) f.value = d[k]; }); }
      rows.appendChild(n);
    };
    var open = function (name, list) {
      rows.innerHTML = ""; form.elements.group.value = name || ""; form.elements.orig.value = name || "";
      (list && list.length ? list : [null]).forEach(addRow);
      $("[data-title]", dlg).textContent = name ? "Edit policy" : "Add policy";
      dlg.showModal();
    };
    $$("[data-policy-new]").forEach(function (b) { b.addEventListener("click", function () { open("", null); }); });
    $$("[data-policy-edit]").forEach(function (b) { b.addEventListener("click", function () { var d = JSON.parse(b.dataset.policyEdit); open(d.name, d.rows); }); });
    $("[data-policy-add]", dlg).addEventListener("click", function () { addRow(null); });
    rows.addEventListener("click", function (e) {
      var b = e.target.closest("[data-policy-del]"); if (!b) return;
      if (rows.children.length > 1) b.closest(".prow").remove(); else $$("input", b.closest(".prow")).forEach(function (i) { i.value = ""; });
    });
  })();

  /* Policy table: search, page size, export */
  (function () {
    var tbl = $("#policy-table"); if (!tbl) return;
    var q = $("#policy-q"), size = $("#policy-size"), info = $("#policy-info");
    var all = $$("tbody.pgrp", tbl);
    var apply = function () {
      var t = q.value.toLowerCase(), lim = parseInt(size.value, 10), shown = 0, match = 0;
      all.forEach(function (r) {
        var ok = !t || r.textContent.toLowerCase().indexOf(t) >= 0;
        if (ok) match++;
        var vis = ok && (lim === 0 || match <= lim);
        r.classList.toggle("hide", !vis); if (vis) shown++;
      });
      info.textContent = "Showing " + (shown ? 1 : 0) + " to " + shown + " of " + match + " records";
    };
    q.addEventListener("input", apply); size.addEventListener("change", apply); apply();
    var text = function (sep) {
      var rows = []; all.filter(function (g) { return !g.classList.contains("hide"); }).forEach(function (g) { rows = rows.concat($$("tr", g)); });
      return rows.map(function (r) {
        return $$("td", r).filter(function (c) { return !c.classList.contains("act"); }).map(function (c) {
          var v = c.textContent.trim().replace(/\s+/g, " "); return sep === "," ? '"' + v.replace(/"/g, '""') + '"' : v;
        }).join(sep);
      }).join("\n");
    };
    $$("[data-export]").forEach(function (b) {
      b.addEventListener("click", function () {
        var k = b.dataset.export;
        if (k === "print") return window.print();
        if (k === "copy") { try { navigator.clipboard.writeText(text("\t")); b.textContent = "Copied"; } catch (e) {} return; }
        var blob = new Blob([text(",")], { type: "text/csv" }), a = document.createElement("a");
        a.href = URL.createObjectURL(blob); a.download = "policies.csv"; a.click();
      });
    });
  })();

  /* Subscribers list: toolbar shortcuts, compact view, column picker, page exports */
  (function () {
    var tbl = $("#subsTable"); if (!tbl) return;
    var bar = $("[data-bulkbar]"), action = $("[data-bulk-action]"), hint = $("#massHint");
    /* Mass Activation / Payment / Delete: open the popup with the ticked customers (their ids go in hidden inputs) */
    $$("[data-mass-open]").forEach(function (b) {
      b.addEventListener("click", function () {
        var dlg = document.getElementById(b.dataset.massOpen); if (!dlg) return;
        var picked = $$(".row-check", tbl).filter(function (c) { return c.checked; });
        if (!picked.length) { if (hint) { hint.classList.remove("hide"); hint.scrollIntoView({ block: "nearest" }); } return; }
        if (hint) hint.classList.add("hide");
        var form = $("form", dlg); form.reset();
        var box = $("[data-ids-box]", dlg); box.innerHTML = "";
        picked.forEach(function (c) { var i = document.createElement("input"); i.type = "hidden"; i.name = "ids"; i.value = c.value; box.appendChild(i); });
        $("[data-ids-label]", dlg).value = picked.length + " selected: " + picked.map(function (c) { return c.dataset.u || c.value; }).join(", ");
        var cf = $("[data-custom-fields]", dlg); if (cf) cf.hidden = true;
        dlg.showModal();
      });
    });
    $$("[data-custom-toggle]").forEach(function (t) {
      t.addEventListener("change", function () {
        var cf = $("[data-custom-fields]", t.closest("form")); if (!cf) return;
        cf.hidden = !t.checked;
        $$("input", cf).forEach(function (i) { i.required = t.checked && i.type === "date"; });
      });
    });
    if (bar) bar.addEventListener("submit", function () { if (hint) hint.classList.add("hide"); });
    var msb = $("#massSetBtn"), msd = $("#mass-dlg");
    if (msb && msd) msb.addEventListener("click", function () {
      var ids = $$(".row-check", tbl).filter(function (c) { return c.checked; }).map(function (c) { return c.value; });
      if (!ids.length) { if (hint) hint.classList.remove("hide"); return; }
      if (hint) hint.classList.add("hide");
      $("#massForm").reset();
      $("#massIds").value = ids.join(",");
      $("#massIdsBox").innerHTML = ids.map(function (i) { return '<input type="hidden" name="ids" value="' + i + '">'; }).join("");
      msd.showModal();
    });
    var cb = $("#compactBtn");
    var setCompact = function (on) { tbl.classList.toggle("compact", on); cb.setAttribute("aria-pressed", on ? "true" : "false"); store("subs.compact", on ? "1" : "0"); };
    if (cb) { cb.addEventListener("click", function () { setCompact(!tbl.classList.contains("compact")); }); var sc = store("subs.compact"); if (sc === "1") setCompact(true); else if (sc === "0") setCompact(false); else if (tbl.classList.contains("compact")) cb.setAttribute("aria-pressed", "true"); }
    var hidden = ["area", "phone"]; try { var sv = store("subs.hidden"); if (sv) hidden = JSON.parse(sv); } catch (e) {}
    /* IP / MAC is shown by default now: once, take it off a list saved while it was hidden by default */
    if (store("subs.ipmac") !== "1") { hidden = hidden.filter(function (k) { return k !== "mac"; }); store("subs.hidden", JSON.stringify(hidden)); store("subs.ipmac", "1"); }
    var applyCols = function () {
      $$("[data-c]", tbl).forEach(function (c) { c.classList.toggle("col-off", hidden.indexOf(c.dataset.c) >= 0); });
      $$("[data-col]").forEach(function (i) { i.checked = hidden.indexOf(i.dataset.col) < 0; });
    };
    applyCols();
    var vb = $("#viewBtn"), vp = $("#viewPop");
    if (vb) {
      vb.addEventListener("click", function (e) { e.stopPropagation(); vp.classList.toggle("hide"); });
      document.addEventListener("click", function (e) { if (!vp.contains(e.target)) vp.classList.add("hide"); });
      $$("[data-col]").forEach(function (i) {
        i.addEventListener("change", function () {
          hidden = hidden.filter(function (k) { return k !== i.dataset.col; });
          if (!i.checked) hidden.push(i.dataset.col);
          store("subs.hidden", JSON.stringify(hidden)); applyCols();
        });
      });
    }
    var grid = function () {
      var head = $$("thead th", tbl), keep = [];
      head.forEach(function (h, i) { if (i > 2 && !h.classList.contains("col-off")) keep.push(i); });
      keep.unshift(0);
      var rows = [keep.map(function (i) { return head[i].textContent.trim(); })];
      $$("tbody tr", tbl).forEach(function (r) {
        var td = $$("td", r);
        rows.push(keep.map(function (i) { return td[i].textContent.replace(/\s+/g, " ").trim(); }));
      });
      return rows;
    };
    var save = function (name, type, text) {
      var a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type: type })); a.download = name; a.click();
    };
    $$("[data-tx]").forEach(function (b) {
      b.addEventListener("click", function () {
        var k = b.dataset.tx, g = grid();
        if (k === "print" || k === "pdf") return window.print();
        if (k === "copy") { try { navigator.clipboard.writeText(g.map(function (r) { return r.join("\t"); }).join("\n")); var o = b.textContent; b.textContent = "Copied"; setTimeout(function () { b.textContent = o; }, 1200); } catch (e) {} return; }
        if (k === "csv") return save("subscribers-page.csv", "text/csv", "\ufeff" + g.map(function (r) { return r.map(function (v) { return '"' + v.replace(/"/g, '""') + '"'; }).join(","); }).join("\n"));
        var esc = function (v) { return v.replace(/&/g, "&amp;").replace(/</g, "&lt;"); };
        save("subscribers-page.xls", "application/vnd.ms-excel", "<html><meta charset='utf-8'><table>" + g.map(function (r) { return "<tr>" + r.map(function (v) { return "<td>" + esc(v) + "</td>"; }).join("") + "</tr>"; }).join("") + "</table></html>");
      });
    });
  })();

  /* generic table exports (Print / Copy / PDF / Excel / CSV) and action dropdowns */
  (function () {
    $$(".xpanel").forEach(function (panel) {
      var tbl = $(".xtable", panel); if (!tbl) return;
      var grid = function () {
        var skip = [], head = $$("thead th", tbl);
        head.forEach(function (h, i) { if (h.classList.contains("no-x")) skip.push(i); });
        var pick = function (cells) { return cells.filter(function (c, i) { return skip.indexOf(i) < 0; }).map(function (c) { return c.textContent.replace(/\s+/g, " ").trim(); }); };
        return [pick(head)].concat($$("tbody tr", tbl).map(function (r) { return pick($$("td", r)); }));
      };
      var save = function (name, type, text) { var a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type: type })); a.download = name; a.click(); };
      $$("[data-xt]", panel).forEach(function (b) {
        b.addEventListener("click", function () {
          var k = b.dataset.xt, g = grid();
          if (k === "print" || k === "pdf") return window.print();
          if (k === "copy") { try { navigator.clipboard.writeText(g.map(function (r) { return r.join("\t"); }).join("\n")); var o = b.textContent; b.textContent = "Copied"; setTimeout(function () { b.textContent = o; }, 1200); } catch (e) {} return; }
          if (k === "csv") return save("export.csv", "text/csv", "\ufeff" + g.map(function (r) { return r.map(function (v) { return '"' + v.replace(/"/g, '""') + '"'; }).join(","); }).join("\n"));
          var esc = function (v) { return v.replace(/&/g, "&amp;").replace(/</g, "&lt;"); };
          save("export.xls", "application/vnd.ms-excel", "<html><meta charset='utf-8'><table>" + g.map(function (r) { return "<tr>" + r.map(function (v) { return "<td>" + esc(v) + "</td>"; }).join("") + "</tr>"; }).join("") + "</table></html>");
        });
      });
    });
    document.addEventListener("click", function (e) {
      $$("details.dd[open]").forEach(function (d) { if (!d.contains(e.target) || e.target.closest(".dd-menu a, .dd-menu button")) d.removeAttribute("open"); });
    });
  })();

  /* ONU pages: link dialog, column chooser, signal chart */
  (function () {
    var mf = document.getElementById("map-form");
    $$("[data-open=map-dlg]").forEach(function (b) {
      b.addEventListener("click", function () {
        var d = JSON.parse(b.dataset.fill || "{}"); if (!mf) return;
        mf.action = d.action || ""; mf.elements.next.value = d.next || "";
      });
    });
    $$("[data-col]").forEach(function (c) {
      c.addEventListener("change", function () {
        var n = c.dataset.col;
        $$(".onu-tbl tr").forEach(function (r) { var cell = r.children[n - 1]; if (cell) cell.style.display = c.checked ? "" : "none"; });
      });
    });
    var host = document.getElementById("onu-chart"), src = document.getElementById("onu-chart-data");
    if (host && src) {
      var NS = "http://www.w3.org/2000/svg", d = JSON.parse(src.textContent), n = d.t.length;
      if (n < 2) { host.innerHTML = '<div class="chart-empty"><div>No history yet.<br>Samples are saved every 5 minutes while the OLT is polled.</div></div>'; return; }
      var W = Math.max(host.clientWidth, 320), H = 280, pl = 44, pr = 44, pt = 14, pb = 28, iw = W - pl - pr, ih = H - pt - pb;
      var s = document.createElementNS(NS, "svg"); s.setAttribute("viewBox", "0 0 " + W + " " + H); s.setAttribute("height", H); host.innerHTML = ""; host.appendChild(s);
      var mk = function (tag, a, p) { var e = document.createElementNS(NS, tag); for (var k in a) e.setAttribute(k, a[k]); (p || s).appendChild(e); return e; };
      var rng = function (arrs) { var v = [].concat.apply([], arrs).filter(function (x) { return x !== null; }); if (!v.length) return [0, 1]; var lo = Math.min.apply(null, v), hi = Math.max.apply(null, v); if (hi - lo < 1) { hi += .5; lo -= .5; } var pad = (hi - lo) * .1; return [lo - pad, hi + pad]; };
      var pw = rng([d.rx, d.tx]), tp = rng([d.temp]);
      var X = function (i) { return pl + iw * i / (n - 1); };
      var YP = function (v) { return pt + ih * (1 - (v - pw[0]) / (pw[1] - pw[0])); };
      var YT = function (v) { return pt + ih * (1 - (v - tp[0]) / (tp[1] - tp[0])); };
      for (var g = 0; g <= 5; g++) {
        var y = pt + ih * g / 5;
        mk("line", { x1: pl, x2: W - pr, y1: y, y2: y, stroke: "currentColor", "stroke-opacity": ".12" });
        var l = mk("text", { x: pl - 6, y: y + 4, "text-anchor": "end", "font-size": 11, fill: "currentColor", "fill-opacity": ".6" }); l.textContent = (pw[1] - (pw[1] - pw[0]) * g / 5).toFixed(1);
        var r = mk("text", { x: W - pr + 6, y: y + 4, "font-size": 11, fill: "currentColor", "fill-opacity": ".6" }); r.textContent = (tp[1] - (tp[1] - tp[0]) * g / 5).toFixed(0);
      }
      var step = Math.ceil(n / 6);
      for (var i = 0; i < n; i += step) { var tx = mk("text", { x: X(i), y: H - 8, "text-anchor": "middle", "font-size": 11, fill: "currentColor", "fill-opacity": ".6" }); tx.textContent = d.t[i]; }
      var cv = function (n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); };
      var series = [["rx", "RX Power (dBm)", cv("--online"), YP], ["tx", "TX Power (dBm)", cv("--accent"), YP], ["temp", "Temperature (°C)", cv("--offline"), YT]];
      var legend = document.getElementById("onu-legend"); if (legend) legend.innerHTML = "";
      series.forEach(function (sr) {
        var pts = [], seg = [];
        d[sr[0]].forEach(function (v, i) { if (v === null) { if (seg.length) pts.push(seg); seg = []; } else seg.push(X(i).toFixed(1) + "," + sr[3](v).toFixed(1)); });
        if (seg.length) pts.push(seg);
        pts.forEach(function (p) { mk("path", { d: "M" + p.join(" L"), fill: "none", stroke: sr[2], "stroke-width": 2 }); });
        if (legend) { var li = document.createElement("span"); li.innerHTML = '<i style="background:' + sr[2] + '"></i>' + sr[1]; legend.appendChild(li); }
      });
      var tip = document.createElement("div"); tip.className = "chart-tip"; host.appendChild(tip);
      s.addEventListener("pointermove", function (e) {
        var bb = s.getBoundingClientRect(), x = (e.clientX - bb.left) * W / bb.width, i = Math.min(Math.max(Math.round((x - pl) / iw * (n - 1)), 0), n - 1);
        var f = function (v, u) { return v === null ? "—" : v + " " + u; };
        tip.innerHTML = "<b>" + d.full[i] + "</b><br>RX " + f(d.rx[i], "dBm") + "<br>TX " + f(d.tx[i], "dBm") + "<br>Temp " + f(d.temp[i], "°C");
        tip.style.display = "block"; tip.style.left = Math.min(X(i) * bb.width / W + 10, bb.width - 150) + "px"; tip.style.top = "10px";
      });
      s.addEventListener("pointerleave", function () { tip.style.display = "none"; });
    }
  })();

  /* live table filter: <input data-filter="#tableId"> */
  (function () {
    $$("[data-filter]").forEach(function (i) {
      i.addEventListener("input", function () {
        var t = $(i.dataset.filter), v = i.value.toLowerCase(); if (!t) return;
        $$("tbody tr", t).forEach(function (r) { r.style.display = r.textContent.toLowerCase().indexOf(v) < 0 ? "none" : ""; });
      });
    });
  })();
  /* v3: bandwidth chart time range (1H / 24H / 7D / 30D). 1H and 24H refresh every 5 minutes, like the samples. */
  $$("[data-traffic-tabs]").forEach(function (box) {
    var host = document.getElementById(box.dataset.target);
    if (!host) return;
    var src = document.getElementById(host.dataset.src), current = "24h";
    function load(rng, btn) {
      if (btn) btn.setAttribute("aria-busy", "true");
      fetch(box.dataset.url + "?range=" + encodeURIComponent(rng), { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d && rng === current) { src.textContent = JSON.stringify(d); if (window.IspCharts) window.IspCharts.draw(host); } })
        .catch(function () { /* offline: keep the chart that is showing */ })
        .then(function () { if (btn) btn.removeAttribute("aria-busy"); });
    }
    $$("button[data-range]", box).forEach(function (b) {
      b.addEventListener("click", function () {
        $$("button[data-range]", box).forEach(function (o) { o.setAttribute("aria-selected", o === b ? "true" : "false"); });
        current = b.dataset.range;
        load(current, b);
      });
    });
    setInterval(function () { if (!document.hidden && (current === "1h" || current === "24h")) load(current); }, 300000);
  });

  /* v3: tables become cards on phones. Every cell gets its column name as data-label; tables without
     data-card hints get them here (first cell = title, next three = key facts, last action cell = buttons).
     The other cells fold away behind a details button. data-cards="off" opts a table out. */
  function cardify(t) {
    if (t.dataset.cards === "off" || t.dataset.cardified) return;
    t.dataset.cardified = "1";
    var heads = $$("thead th", t).map(function (th) { return th.textContent.replace(/[↑↓]/g, "").trim(); });
    var hinted = !!t.querySelector("td[data-card]");
    $$("tbody > tr", t).forEach(function (tr) {
      var cells = Array.prototype.slice.call(tr.children), keys = 0;
      cells.forEach(function (td, i) {
        if (!td.hasAttribute("data-label") && heads[i]) td.setAttribute("data-label", heads[i]);
        if (hinted) return;
        if (td.colSpan > 1 || cells.length === 1) { td.dataset.card = "title"; return; }
        if (td.classList.contains("sel")) td.dataset.card = "select";
        else if (td.classList.contains("act") || (i === cells.length - 1 && td.querySelector("form, a.btn, button"))) td.dataset.card = "actions";
        else if (!tr.querySelector("td[data-card=title]")) td.dataset.card = "title";
        else if (keys < 3) { td.dataset.card = "key"; keys++; }
      });
      var folded = cells.filter(function (td) { return !td.dataset.card && td.textContent.trim(); });
      var title = tr.querySelector("td[data-card=title]");
      if (!folded.length || !title) return;
      var b = document.createElement("button");
      b.type = "button"; b.className = "card-toggle"; b.setAttribute("aria-expanded", "false"); b.setAttribute("aria-label", "Show details");
      b.innerHTML = '<svg class="i" aria-hidden="true"><use href="#i-chevron-down"/></svg>';
      b.addEventListener("click", function (e) {
        e.preventDefault(); e.stopPropagation();
        var open = tr.classList.toggle("open");
        b.setAttribute("aria-expanded", open ? "true" : "false");
        b.setAttribute("aria-label", open ? "Hide details" : "Show details");
      });
      title.appendChild(b);
    });
  }
  $$("table.tbl").forEach(cardify);

  /* Phase 2 shell: keyboard shortcuts (DESIGN.md B10) and bottom-nav scroll behaviour */
  document.addEventListener("keydown", function (e) {
    var t = e.target, typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
    if (e.key === "Escape") { document.body.classList.remove("nav-open"); return; }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "/") { var s = $("[data-global-search]"); if (s) { e.preventDefault(); s.focus(); } }
    else if (e.key === "[") { var b = $("[data-side-toggle]"); if (b) b.click(); }
  });
  /* filter dialogs (Payments, Cashflow): a second list that follows the first (data-follows="name of the first"),
     only filled fields go in the address, and Copy URL gives the filtered page's link */
  $$("form[data-filter-form]").forEach(function (form) {
    $$("select[data-follows]", form).forEach(function (sel) {
      var lead = form.elements[sel.dataset.follows];
      if (!lead) return;
      var sync = function () {
        var any = false;
        $$("option", sel).forEach(function (o) {
          if (!o.value) return;
          var show = o.dataset.tier === lead.value;
          o.hidden = !show; o.disabled = !show; if (show) any = true;
          if (!show && o.selected) sel.value = "";
        });
        sel.disabled = !any;
      };
      lead.addEventListener("change", function () { sel.value = ""; sync(); });
      form.addEventListener("synced", sync);
      sync();
    });
    window.addEventListener("pageshow", function () {     /* Back button: the fields switched off on submit come back */
      $$("[name]", form).forEach(function (el) { el.disabled = false; });
      form.dispatchEvent(new Event("synced"));
    });
    form.addEventListener("submit", function () {
      $$("[name]", form).forEach(function (el) { if (!el.value) el.disabled = true; });
    });
    $$("[data-copy-url]", form).forEach(function (b) {
      b.addEventListener("click", function () {
        var p = new URLSearchParams();
        $$("[name]", form).forEach(function (el) { if (el.value && !el.disabled) p.append(el.name, el.value); });
        var url = location.origin + form.getAttribute("action") + (p.toString() ? "?" + p.toString() : "");
        var o = b.innerHTML, done = function () { b.textContent = "Copied"; setTimeout(function () { b.innerHTML = o; }, 1200); };
        if (navigator.clipboard) navigator.clipboard.writeText(url).then(done, done); else done();
      });
    });
  });
  /* a text box whose suggestions follow another field (Cashflow: the categories of the chosen type) */
  $$("input[data-list-by]").forEach(function (inp) {
    var form = inp.form, lead = form && form.elements[inp.dataset.listBy];
    if (!lead) return;
    var sync = function () { inp.setAttribute("list", inp.dataset.listPrefix + lead.value); };
    lead.addEventListener("change", sync);
    form.addEventListener("filled", sync);
    sync();
  });
  /* Cashflow: compact rows, remembered on this browser */
  $$("[data-compact-for]").forEach(function (b) {
    var tbl = document.getElementById(b.dataset.compactFor), key = "compact." + b.dataset.compactFor;
    if (!tbl) return;
    var set = function (on) { tbl.classList.toggle("compact", on); b.setAttribute("aria-pressed", on ? "true" : "false"); store(key, on ? "1" : "0"); };
    b.addEventListener("click", function () { set(!tbl.classList.contains("compact")); });
    if (store(key) === "1") set(true);
  });
  var bn = $(".bottom-nav"), lastY = window.scrollY || 0;
  var bnTick = false;                 /* at most once per frame: smooth scrolling on phones */
  if (bn) window.addEventListener("scroll", function () {
    if (bnTick) return;
    bnTick = true;
    requestAnimationFrame(function () {
      bnTick = false;
      var y = window.scrollY || 0;
      if (Math.abs(y - lastY) > 8) { bn.classList.toggle("bn-hidden", y > lastY && y > 120); lastY = y; }
    });
  }, { passive: true });
})();

/* subscriber page > Address Information > Location: "Use my location" fills the box from this phone's GPS
   (the technician standing at the connection); the person still presses Save */
(function () {
  document.querySelectorAll("[data-loc-here]").forEach(function (b) {
    b.addEventListener("click", function () {
      var box = b.form && b.form.querySelector("[data-loc-input]");
      if (!box) return;
      if (!navigator.geolocation) { (window.panelAlert || alert)("This browser cannot read the location."); return; }
      var label = b.innerHTML; b.disabled = true; b.textContent = "Finding…";
      navigator.geolocation.getCurrentPosition(function (p) {
        box.value = p.coords.latitude.toFixed(6) + ", " + p.coords.longitude.toFixed(6);
        b.disabled = false; b.innerHTML = label; box.focus();
      }, function (err) {
        b.disabled = false; b.innerHTML = label;
        (window.panelAlert || alert)(err && err.code === 1 ? "Location permission was refused. Allow location for this site in the browser, or paste a Google Maps link."
          : "Could not read the location (" + (err && err.message || "no GPS signal") + "). Try again outside, or paste a Google Maps link.");
      }, { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
    });
  });
})();
