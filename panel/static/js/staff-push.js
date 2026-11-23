/* Staff app job alerts (mod_staffpush.py): the "Turn on job alerts" card ([data-staff-push]), keeping this phone's
   push subscription fresh, and a toast when a job arrives while the panel is open. */
(function () {
  var me = document.currentScript;
  if (!me) return;
  var keyUrl = me.dataset.key, subUrl = me.dataset.sub, unsubUrl = me.dataset.unsub, testUrl = me.dataset.test, csrf = me.dataset.csrf || "";
  var canPush = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window && window.isSecureContext;
  var ios = /iPhone|iPad|iPod/.test(navigator.userAgent);
  var standalone = (window.matchMedia && matchMedia("(display-mode: standalone)").matches) || navigator.standalone === true;

  function post(url, data) {
    return fetch(url, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
                        body: JSON.stringify(data || {}) }).then(function (r) { return r.json(); });
  }
  function keyBytes(b64) {
    var pad = "=".repeat((4 - b64.length % 4) % 4), raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function subscribe(first) {
    return fetch(keyUrl, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.key) throw new Error("no key");
      return navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(function () { return navigator.serviceWorker.ready; }).then(function (reg) {
        return reg.pushManager.getSubscription().then(function (old) {
          return old || reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(d.key) });
        });
      }).then(function (sub) { var j = sub.toJSON(); j.first = !!first; return post(subUrl, j); });
    });
  }
  function unsubscribe() {
    return navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription(); }).then(function (sub) {
      if (!sub) return null;
      return post(unsubUrl, { endpoint: sub.endpoint }).then(function () { return sub.unsubscribe(); });
    });
  }
  function toast(title, body, url) {
    var t = document.createElement("a");
    t.className = "sp-toast"; t.href = url || "#"; t.setAttribute("role", "status");
    t.innerHTML = "<b></b><span></span>";
    t.querySelector("b").textContent = title; t.querySelector("span").textContent = body || "";
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.classList.add("on"); });
    setTimeout(function () { t.classList.remove("on"); setTimeout(function () { t.remove(); }, 400); }, 9000);
  }

  function state() {
    if (!canPush) return ios && !standalone ? "ios-install" : "none";
    if (Notification.permission === "denied") return "denied";
    return Notification.permission === "granted" ? "on" : "ask";
  }
  function render(card) {
    var s = state(), html = "";
    if (s === "ask") html = '<div class="sp-txt"><b>Get a notification for every new job</b><span>Your phone rings when a job is given to you, even when the app is closed.</span></div><button type="button" class="btn primary" data-sp-on>Turn on job alerts</button>';
    else if (s === "on") html = '<div class="sp-txt"><b>Job alerts are on for this phone</b><span>A new job shows on the lock screen with the phone\'s notification sound.</span></div><button type="button" class="btn sm" data-sp-test>Send a test</button><button type="button" class="btn sm" data-sp-off>Turn off</button>';
    else if (s === "denied") html = '<div class="sp-txt"><b>Notifications are blocked for this panel</b><span>Allow them: touch and hold the app icon &gt; App info &gt; Notifications &gt; Allow (Chrome: ⋮ &gt; Settings &gt; Site settings &gt; Notifications). Then reload this page.</span></div>';
    else if (s === "ios-install") html = '<div class="sp-txt"><b>Install the app first</b><span>On iPhone, job alerts work in the installed app: Share &gt; Add to Home Screen, then open it from the home screen.</span></div>';
    else { card.hidden = true; return; }
    card.hidden = false;
    card.classList.toggle("on", s === "on");
    card.innerHTML = html;
  }
  var cards = document.querySelectorAll("[data-staff-push]");
  cards.forEach(render);
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-sp-on],[data-sp-off],[data-sp-test]");
    if (!b) return;
    b.disabled = true;
    if (b.hasAttribute("data-sp-on")) {
      Notification.requestPermission().then(function (p) {
        if (p !== "granted") { cards.forEach(render); return; }
        return subscribe(true).then(function () { cards.forEach(render); toast("Job alerts are on", "You will get a notification for each new job."); });
      }).catch(function () { b.disabled = false; toast("Could not turn on job alerts", "Try again, or check the phone's notification settings."); });
    } else if (b.hasAttribute("data-sp-off")) {
      unsubscribe().catch(function () {}).then(function () {
        cards.forEach(function (c) { c.classList.remove("on"); c.innerHTML = '<div class="sp-txt"><b>Job alerts are off</b><span>Reload the page to turn them on again.</span></div>'; });
      });
    } else {
      subscribe(false).then(function () { return post(testUrl); }).then(function (d) {
        b.disabled = false;
        toast(d && d.ok ? "Test sent" : "No phone is switched on", d && d.ok ? "It arrives in a few seconds." : "Turn job alerts on first.");
      }).catch(function () { b.disabled = false; });
    }
  });
  if (canPush && Notification.permission === "granted") subscribe(false).catch(function () {});   /* keep the server copy fresh */
  if ("serviceWorker" in navigator) navigator.serviceWorker.addEventListener("message", function (e) {
    if (e.data && e.data.type === "staff-push") toast(e.data.title, e.data.body, e.data.url);
  });
})();
