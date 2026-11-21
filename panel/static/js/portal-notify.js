/* Customer portal / app: live in-app notifications.
   Every 45 s (while the app is open and visible) asks the panel for the unread count. It updates the red numbers on
   the bell, the app icon badge (installed app, where the phone supports it) and pops a small card when a new
   notification arrives. Tapping the card opens the list. */
(function () {
  var me = document.currentScript, url = me.getAttribute("data-check"), list = me.getAttribute("data-list");
  var unread = parseInt(me.getAttribute("data-unread") || "0", 10) || 0, seen = null;
  try { seen = parseInt(sessionStorage.getItem("notif-seen") || "", 10) || null; } catch (e) {}

  function paint(n) {
    document.querySelectorAll("[data-notif-count]").forEach(function (b) {
      b.textContent = n > 99 ? "99+" : String(n);
      b.hidden = !n;
    });
    if ("setAppBadge" in navigator) { try { n ? navigator.setAppBadge(n) : navigator.clearAppBadge(); } catch (e) {} }
  }
  function toast(item) {
    var t = document.createElement("a");
    t.className = "notif-toast";
    t.href = list;
    t.setAttribute("role", "status");
    t.innerHTML = '<span class="notif-toast-ic" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg></span><span class="notif-toast-txt"><b></b><small></small></span>';
    t.querySelector("b").textContent = item.title || "";
    t.querySelector("small").textContent = item.body || "";
    document.body.appendChild(t);
    requestAnimationFrame(function () { requestAnimationFrame(function () { t.classList.add("in"); }); });
    setTimeout(function () { t.classList.remove("in"); setTimeout(function () { t.remove(); }, 400); }, 6000);
    if (navigator.vibrate) { try { navigator.vibrate(60); } catch (e) {} }
  }
  function check() {
    if (document.hidden) return;
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" }, cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        paint(d.unread || 0);
        if (d.latest && d.latest.id && d.latest.id !== seen && (seen !== null || d.unread > unread)
            && location.pathname !== new URL(list, location.href).pathname) toast(d.latest);
        if (d.latest && d.latest.id) { seen = d.latest.id; try { sessionStorage.setItem("notif-seen", String(seen)); } catch (e) {} }
        unread = d.unread || 0;
      }).catch(function () {});
  }
  paint(unread);
  setTimeout(check, 1500);
  setInterval(check, 45000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });
})();
