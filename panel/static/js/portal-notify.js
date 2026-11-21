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
  /* a short two-note chime made in the browser (no sound file); browsers allow it after the first tap on the page */
  var actx = null;
  function chime() {
    try {
      actx = actx || new (window.AudioContext || window.webkitAudioContext)();
      if (actx.state === "suspended") actx.resume();
      [[880, 0], [1318.5, 0.14]].forEach(function (n) {
        var o = actx.createOscillator(), g = actx.createGain(), t = actx.currentTime + n[1];
        o.type = "sine"; o.frequency.value = n[0];
        g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.25, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.35);
        o.connect(g); g.connect(actx.destination); o.start(t); o.stop(t + 0.4);
      });
    } catch (e) {}
  }
  document.addEventListener("pointerdown", function unlock() {
    try { actx = actx || new (window.AudioContext || window.webkitAudioContext)(); actx.resume(); } catch (e) {}
    document.removeEventListener("pointerdown", unlock);
  });
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
    chime();
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
  /* a push arrived while the app is open: the service worker tells us; show it here too, with the chime */
  if ("serviceWorker" in navigator) navigator.serviceWorker.addEventListener("message", function (ev) {
    var m = ev.data || {};
    if (m.type !== "push") return;
    if (typeof m.unread === "number") { paint(m.unread); unread = m.unread; }
    var id = parseInt(String(m.tag || "").replace(/^n/, ""), 10);
    if (id) { seen = id; try { sessionStorage.setItem("notif-seen", String(id)); } catch (e) {} }
    toast({ title: m.title, body: m.body });
  });

  /* ---- Web Push opt-in ---- */
  var keyUrl = me.getAttribute("data-push-key"), subUrl = me.getAttribute("data-push-sub"), unsubUrl = me.getAttribute("data-push-unsub");
  var csrf = me.getAttribute("data-csrf") || "";
  var ua = navigator.userAgent || "", ios = /iphone|ipad|ipod/i.test(ua) || (/macintosh/i.test(ua) && navigator.maxTouchPoints > 1);
  var standalone = (window.matchMedia && matchMedia("(display-mode: standalone)").matches) || navigator.standalone === true;
  var canPush = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  function post(url, body) {
    return fetch(url, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body) }).then(function (r) { return r.ok; });
  }
  function keyBytes(b64) {
    var s = (b64 + "===".slice((b64.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/"), raw = atob(s), out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function subscribe() {
    return fetch(keyUrl, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.key) throw new Error("no key");
      return navigator.serviceWorker.ready.then(function (reg) {
        return reg.pushManager.getSubscription().then(function (old) {
          return old || reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(d.key) });
        });
      });
    }).then(function (sub) { return post(subUrl, sub.toJSON()); });
  }
  function unsubscribe() {
    return navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription(); }).then(function (sub) {
      if (!sub) return true;
      return post(unsubUrl, { endpoint: sub.endpoint }).then(function () { return sub.unsubscribe(); });
    });
  }
  var LATER = "push-card-later";
  function laterHome() { try { var t = +localStorage.getItem(LATER); return t && Date.now() - t < 7 * 864e5; } catch (e) { return false; } }
  function render(card, state) {
    var where = card.getAttribute("data-push-card");
    var bell = '<span class="push-ic" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg></span>';
    var html = "";
    if (state === "ask" && !(where === "home" && laterHome())) {
      html = bell + '<div class="push-txt"><b>নোটিফিকেশন চালু করুন</b><span>অ্যাপ বন্ধ থাকলেও পেমেন্ট আর মেয়াদ শেষের খবর ফোনে আসবে, সাউন্ডসহ।</span></div>' +
        '<div class="push-acts">' + (where === "home" ? '<button type="button" class="btn sm" data-push-later>পরে</button>' : "") +
        '<button type="button" class="btn sm primary" data-push-on>চালু করুন</button></div>';
    } else if (state === "ios-install") {
      html = bell + '<div class="push-txt"><b>iPhone-এ নোটিফিকেশন পেতে</b><span>আগে অ্যাপটি ইন্সটল করুন: Safari-র Share বোতাম &rarr; Add to Home Screen, তারপর হোম স্ক্রিনের অ্যাপ থেকে খুলুন।</span></div>';
    } else if (state === "on" && where === "list") {
      html = bell + '<div class="push-txt"><b>ফোনে নোটিফিকেশন চালু আছে</b><span>অ্যাপ বন্ধ থাকলেও খবর আসবে।</span></div><div class="push-acts"><button type="button" class="btn sm" data-push-off>বন্ধ করুন</button></div>';
      card.classList.add("on");
    } else if (state === "denied" && where === "list") {
      html = bell + '<div class="push-txt"><b>নোটিফিকেশন বন্ধ করা আছে</b><span>চালু করতে ব্রাউজার বা ফোনের সেটিংসে এই সাইটের Notifications অনুমতি দিন।</span></div>';
    }
    card.innerHTML = html;
    card.hidden = !html;
  }
  function refresh() {
    var cards = document.querySelectorAll("[data-push-card]");
    if (!cards.length) return;
    var state;
    if (!canPush) state = ios && !standalone ? "ios-install" : "none";
    else if (Notification.permission === "denied") state = "denied";
    else if (Notification.permission === "granted") state = "on";
    else state = "ask";
    cards.forEach(function (c) { render(c, state); });
  }
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-push-on]")) {
      Notification.requestPermission().then(function (p) {
        if (p === "granted") return subscribe().then(function () { toast({ title: "নোটিফিকেশন চালু হয়েছে", body: "এখন থেকে অ্যাপ বন্ধ থাকলেও খবর পাবেন।" }); });
      }).catch(function () {}).then(refresh);
    } else if (e.target.closest("[data-push-off]")) {
      unsubscribe().catch(function () {}).then(function () {
        document.querySelectorAll('[data-push-card="list"]').forEach(function (c) {
          c.classList.remove("on");
          c.innerHTML = '<div class="push-txt"><b>নোটিফিকেশন বন্ধ করা হয়েছে</b><span>আবার চালু করতে পেজটি রিফ্রেশ করে "চালু করুন" চাপুন।</span></div>';
        });
      });
    } else if (e.target.closest("[data-push-later]")) {
      try { localStorage.setItem(LATER, String(Date.now())); } catch (x) {}
      refresh();
    }
  });
  if (canPush && Notification.permission === "granted") {
    subscribe().catch(function () {});       /* keep the server's copy fresh (new phone data, a changed key) */
  }
  refresh();

  paint(unread);
  setTimeout(check, 1500);
  setInterval(check, 45000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });
})();
