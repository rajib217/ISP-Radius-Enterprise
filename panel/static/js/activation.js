/* Subscriber Activation window: search the customer, pick the package, show profile and package details. */
(function () {
  var form = document.getElementById("act-form");
  if (!form) return;
  var $ = function (id) { return document.getElementById(id); };
  var user = $("act-user"), plan = $("act-plan"), list = $("act-subs"), amt = $("act-amt");
  var chosen = null, timer = null, seq = 0, quote = null;   // quote: {price, balance} for the chosen customer + package
  var cur = form.dataset.currency || "";

  function money(n) { return Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }

  // --- toggles
  function syncToggles() {
    var custom = !!($("act-custom") && $("act-custom").checked), paying = $("act-paytog").checked;
    $("act-exp-box").hidden = !custom;
    $("act-exp").disabled = !custom; $("act-exp").required = custom;
    $("act-pay").hidden = !paying;
    ["act-method", "act-amt", "act-note"].forEach(function (i) { $(i).disabled = !paying; });
    var fromBal = fromBalance();
    amt.disabled = !paying || fromBal;            // paid from the balance: nothing is received
    amt.required = paying && !fromBal;
    showMoney();
  }
  function fromBalance() { return !$("act-paytog").checked || $("act-method").value === "balance"; }

  // --- what the customer pays and how their balance changes (server rules: mod_subs.subscriber_activate)
  function showMoney() {
    var box = $("act-money");
    box.classList.remove("bad");
    if (!quote || quote.price === null) { box.hidden = true; return; }
    var p = Number(quote.price), b = Number(quote.balance), txt, after;
    if (fromBalance()) {
      after = b - p;
      txt = "Package price " + cur + " " + money(p) + " is taken from the subscriber balance (" + cur + " " + money(b) + " → " + cur + " " + money(after) + ").";
    } else {
      var diff = Number(amt.value || 0) - p;
      after = b + diff;
      if (Math.abs(diff) < 0.005) txt = "Package price " + cur + " " + money(p) + ". Subscriber balance stays " + cur + " " + money(b) + ".";
      else if (diff > 0) txt = "Package price " + cur + " " + money(p) + ". The extra " + cur + " " + money(diff) + " goes to the subscriber balance (" + cur + " " + money(b) + " → " + cur + " " + money(after) + ").";
      else txt = "Package price " + cur + " " + money(p) + ". The missing " + cur + " " + money(-diff) + " is taken from the subscriber balance (" + cur + " " + money(b) + " → " + cur + " " + money(after) + ").";
    }
    if (after < -0.005) { txt += " The balance is too low: take more cash or add balance first."; box.classList.add("bad"); }
    box.textContent = txt;
    box.hidden = false;
  }
  if ($("act-custom")) $("act-custom").addEventListener("change", syncToggles);
  $("act-paytog").addEventListener("change", syncToggles);

  // --- profile card
  function showProfile() {
    var s = chosen;
    if ($("act-info")) $("act-info").hidden = !s;
    $("inf-avatar").textContent = s ? s.username.charAt(0).toUpperCase() : "?";
    $("inf-user").textContent = s ? s.username : "—";
    $("inf-id").textContent = s ? "@" + s.id : "";
    var st = $("inf-state");
    st.textContent = "";
    if (s) {
      var b = document.createElement("span");
      b.className = "badge " + (s.state === "active" ? "ok" : s.state === "expired" ? "warn" : "bad");
      b.textContent = s.state.charAt(0).toUpperCase() + s.state.slice(1);
      st.appendChild(b);
    } else st.textContent = "—";
    $("inf-exp").textContent = s ? (s.expiry ? "Expires " + s.expiry : "No expiry date") : "Account Status";
  }

  // --- package card, amount and what the seller pays
  function showPackage() {
    var o = plan.options[plan.selectedIndex];
    var has = o && o.value;
    $("pk-name").textContent = has ? o.dataset.name : "Package";
    if (!has) { $("pk-dur").textContent = "—"; $("pk-time").textContent = "—"; return; }
    var days = parseInt(o.dataset.days, 10) || 0, n = parseInt(o.dataset.dur, 10) || 0, t = o.dataset.dtype;
    var dur;
    if (n && (t === "month" || t === "year")) dur = plural(n, t === "year" ? "Year" : "Month");
    else if (days >= 30 && days % 30 === 0) dur = plural(days / 30, "Month");
    else dur = plural(days, "Day");
    $("pk-dur").textContent = dur;
    $("pk-time").textContent = plural(days, "Day") + " " + plural(days * 24, "Hour");
    if (!amt.disabled && !quote) amt.value = o.dataset.price || 0;
  }
  function showCharge() {
    var box = $("act-charge");
    box.hidden = true; box.classList.remove("bad");
    quote = null; showMoney();
    if (!chosen || !plan.value) return;
    var my = ++seq;
    var custom = $("act-custom") && $("act-custom").checked && $("act-exp").value;
    fetch(form.dataset.charge + "?u=" + encodeURIComponent(chosen.username) + "&plan=" + encodeURIComponent(plan.value) +
          (custom ? "&exp=" + encodeURIComponent($("act-exp").value) : ""), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (my !== seq) return;
        if (d.price !== null && d.price !== undefined) {
          quote = { price: d.price, balance: d.balance || 0 };
          if (!amt.disabled) amt.value = Number(d.price).toFixed(2);
          showMoney();
        }
        if (!d.charge) return;
        var c = d.charge;
        box.textContent = (c.mine ? "Takes " : c.who + " pays ") + cur + " " + money(c.cost) + (c.mine ? " from your balance" : " from their balance") +
          " (balance " + cur + " " + money(c.balance) + ")." + (c.low ? " Balance is too low." : "");
        if (c.low) box.classList.add("bad");
        box.hidden = false;
      }).catch(function () {});
  }
  function minExpiry() {
    var d = new Date(); d.setDate(d.getDate() + 1);
    if (chosen && chosen.expiry_iso) { var e = new Date(chosen.expiry_iso); e.setDate(e.getDate() + 1); if (e > d) d = e; }
    $("act-exp").min = d.toISOString().slice(0, 10);
  }

  function choose(s) {
    chosen = s;
    if (s && s.plan_id) {
      for (var i = 0; i < plan.options.length; i++) if (plan.options[i].value === String(s.plan_id)) { plan.selectedIndex = i; break; }
    }
    showProfile(); showPackage(); showCharge(); minExpiry();
  }

  // --- search as you type (a list under the box: username, name, phone, status)
  var box = $("act-list"), items = [], hi = -1;
  function open(on) { box.hidden = !on; user.setAttribute("aria-expanded", on ? "true" : "false"); }
  function row(text, cls) { var li = document.createElement("li"); li.className = cls || "none"; li.textContent = text; return li; }
  function render(rows, msg) {
    items = rows; hi = rows.length ? 0 : -1; box.textContent = "";
    if (!rows.length) box.appendChild(row(msg || "No subscriber found."));
    rows.forEach(function (s, i) {
      var li = document.createElement("li"), g = document.createElement("span"), b = document.createElement("b"), sm = document.createElement("small"), bd = document.createElement("span");
      li.setAttribute("role", "option"); g.className = "grow"; b.textContent = s.username;
      sm.textContent = [s.name, s.phone].filter(Boolean).join(" \u00b7 ");
      bd.className = "badge " + (s.state === "active" ? "ok" : s.state === "expired" ? "warn" : "bad");
      bd.textContent = s.state.charAt(0).toUpperCase() + s.state.slice(1);
      g.appendChild(b); if (sm.textContent) g.appendChild(sm); li.appendChild(g); li.appendChild(bd);
      li.addEventListener("mousedown", function (e) { e.preventDefault(); pick(s); });
      box.appendChild(li);
    });
    mark(); open(true);
    var typed = user.value.trim().toLowerCase();
    var exact = rows.filter(function (s) { return s.username.toLowerCase() === typed; })[0];
    if (exact && !chosen) choose(exact);
  }
  function mark() {
    Array.prototype.forEach.call(box.querySelectorAll("li[role=option]"), function (li, i) {
      li.classList.toggle("on", i === hi); li.setAttribute("aria-selected", i === hi ? "true" : "false");
      if (i === hi && li.scrollIntoView) li.scrollIntoView({ block: "nearest" });
    });
  }
  function pick(s) { user.value = s.username; user.setCustomValidity(""); open(false); choose(s); }
  function search(v) {
    var my = ++seq2;
    return fetch(form.dataset.lookup + "?q=" + encodeURIComponent(v), { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) { return my === seq2 ? (d.items || []) : null; });
  }
  var seq2 = 0;
  user.addEventListener("input", function () {
    user.setCustomValidity("");
    if (chosen && user.value.trim().toLowerCase() !== chosen.username.toLowerCase()) choose(null);
    clearTimeout(timer);
    var v = user.value.trim();
    if (v.length < 2) { open(false); return; }
    timer = setTimeout(function () {
      search(v).then(function (rows) { if (rows) render(rows); })
        .catch(function () { render([], "Search failed. Reload the page and try again."); });
    }, 200);
  });
  user.addEventListener("keydown", function (e) {
    if (box.hidden || !items.length) return;
    if (e.key === "ArrowDown") { hi = (hi + 1) % items.length; mark(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { hi = (hi - 1 + items.length) % items.length; mark(); e.preventDefault(); }
    else if (e.key === "Enter" && hi >= 0) { e.preventDefault(); pick(items[hi]); }
    else if (e.key === "Escape") { open(false); }
  });
  user.addEventListener("blur", function () { setTimeout(function () { open(false); }, 120); });
  user.addEventListener("focus", function () { if (items.length && !chosen && user.value.trim().length >= 2) open(true); });
  plan.addEventListener("change", function () { showPackage(); showCharge(); });

  // Activate: a username that was typed in full (not picked from the list) is looked up first, then the form is sent.
  var submitting = false;
  var printing = false;
  function send() { $("act-print").disabled = !printing; submitting = true; form.submit(); }
  function needPackage() {
    if (plan.value) return false;
    plan.setCustomValidity("Select a package."); plan.reportValidity(); return true;
  }
  plan.addEventListener("change", function () { plan.setCustomValidity(""); });
  form.addEventListener("submit", function (e) {
    if (submitting) return;
    e.preventDefault();
    printing = !!(e.submitter && e.submitter.name === "print");
    if (chosen) { if (!needPackage()) send(); return; }
    var v = user.value.trim();
    if (!v) { user.reportValidity(); return; }
    search(v).then(function (rows) {
      var hit = (rows || []).filter(function (s) { return s.username.toLowerCase() === v.toLowerCase(); })[0];
      if (!hit) { user.setCustomValidity("No subscriber with that username. Pick one from the list."); user.reportValidity(); return; }
      pick(hit);
      if (!needPackage()) send();
    }).catch(function () { user.setCustomValidity("Could not check the username. Reload the page and try again."); user.reportValidity(); });
  });
  $("act-paytog").addEventListener("change", function () { if (this.checked) showPackage(); showMoney(); });
  $("act-method").addEventListener("change", syncToggles);
  amt.addEventListener("input", showMoney);
  $("act-exp").addEventListener("change", showCharge);
  if ($("act-custom")) $("act-custom").addEventListener("change", showCharge);

  syncToggles();
  if (user.value) {
    search(user.value.trim()).then(function (rows) {
      var h = (rows || []).filter(function (s) { return s.username.toLowerCase() === user.value.trim().toLowerCase(); })[0];
      if (h) choose(h);
    }).catch(function () {});
  } else showProfile();
  showPackage();
})();
