/* Installable app (see mod_pwa.py and templates/_pwa_head.html):
   - registers the service worker (fast static files, offline page)
   - fades out the launch splash once the page has loaded (shown for at least 0.9 s so it does not flicker)
   - a thin loading bar at the top while the next page loads
   - an "Install app" button (data-pwa-install) when the browser offers installation */
(function () {
  var html = document.documentElement;

  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
    window.addEventListener("load", function () { navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {}); });
  }

  /* ---- splash ---- */
  if (html.classList.contains("pwa-launch")) {
    var started = Date.now();
    var hide = function () {
      setTimeout(function () {
        html.classList.add("pwa-out");
        setTimeout(function () { html.classList.remove("pwa-launch", "pwa-out"); }, 480);
      }, Math.max(0, 900 - (Date.now() - started)));
    };
    if (document.readyState === "complete") hide(); else window.addEventListener("load", hide);
  }

  /* ---- loading bar ---- */
  var bar = document.createElement("div");
  bar.className = "pwa-progress";
  bar.setAttribute("aria-hidden", "true");
  bar.innerHTML = "<i></i>";
  document.body.appendChild(bar);
  var stopTimer = null;
  function start() {
    bar.classList.remove("done");
    void bar.offsetWidth;                       /* restart the animation */
    bar.classList.add("on");
    clearTimeout(stopTimer);
    stopTimer = setTimeout(stop, 10000);        /* downloads and blocked forms never unload the page */
  }
  function stop() { clearTimeout(stopTimer); bar.classList.remove("on"); bar.classList.add("done"); }
  window.addEventListener("pageshow", stop);
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || a.target === "_blank" || a.hasAttribute("download") || a.hasAttribute("data-open")) return;
    var href = a.getAttribute("href");
    if (!href || href.charAt(0) === "#" || /^(mailto|tel|javascript):/i.test(href)) return;
    var url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;   /* same-page anchor */
    start();
  });
  document.addEventListener("submit", function (e) {
    setTimeout(function () { if (!e.defaultPrevented && !(e.target.target === "_blank")) start(); }, 0);
  });

  /* ---- install: the sidebar button, and a bottom sheet on phones ----
     Android / Chrome / Samsung Internet: the browser's own install prompt (beforeinstallprompt), one tap.
     iPhone / iPad: Safari has no install prompt, so the sheet shows the two steps (Share > Add to Home Screen).
     "Later" hides it for 7 days; once installed (or opened as the app) it never shows. */
  var kindMeta = document.querySelector('meta[name="pwa-kind"]');
  var KIND = (kindMeta && kindMeta.content) || "panel";          /* panel and portal are two separate apps */
  var deferred = null, KEY = "pwa-install-later-" + KIND, WAIT = 7 * 24 * 3600 * 1000;
  var standalone = (window.matchMedia && matchMedia("(display-mode: standalone)").matches) || navigator.standalone === true;
  var ua = navigator.userAgent || "";
  var ios = /iphone|ipad|ipod/i.test(ua) || (/macintosh/i.test(ua) && navigator.maxTouchPoints > 1);
  var phone = (window.matchMedia && matchMedia("(max-width: 900px)").matches) && (navigator.maxTouchPoints > 0 || "ontouchstart" in window);
  function later() { try { var t = +localStorage.getItem(KEY); return t && Date.now() - t < WAIT; } catch (e) { return false; } }
  function remember(ms) { try { localStorage.setItem(KEY, String(Date.now() + (ms || 0))); } catch (e) {} }

  function appInfo() {
    var t = document.querySelector('meta[name="apple-mobile-web-app-title"]');
    var i = document.querySelector('link[rel="apple-touch-icon"]');
    return { name: (t && t.content) || document.title, icon: i ? i.href : "" };
  }
  var sheet = null;
  function closeSheet() {
    if (!sheet) return;
    var s = sheet; sheet = null;
    s.classList.remove("in");
    setTimeout(function () { s.remove(); }, 320);
  }
  function showSheet(mode) {
    if (sheet || standalone) return;
    var info = appInfo();
    var share = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg>';
    var plus = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="4"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>';
    var body = mode === "ios"
      ? '<ol class="pwa-steps"><li><span class="pwa-step-ic">' + share + '</span><span>নিচের <b>Share</b> বোতামে চাপুন</span></li>' +
        '<li><span class="pwa-step-ic">' + plus + '</span><span><b>Add to Home Screen</b> বেছে নিন</span></li></ol>'
      : '<ul class="pwa-perks"><li>হোম স্ক্রিন থেকে এক ট্যাপে খুলুন</li><li>দ্রুত লোড হয়, পুরো স্ক্রিন জুড়ে</li><li>কোনো স্টোর লাগবে না, জায়গাও লাগে খুব কম</li></ul>';
    var acts = mode === "ios"
      ? '<button type="button" class="pwa-btn ghost" data-pwa-later>পরে</button><button type="button" class="pwa-btn primary" data-pwa-ok>বুঝেছি</button>'
      : '<button type="button" class="pwa-btn ghost" data-pwa-later>পরে</button><button type="button" class="pwa-btn primary" data-pwa-go>ইন্সটল করুন</button>';
    sheet = document.createElement("div");
    sheet.className = "pwa-sheet" + (mode === "ios" ? " ios" : "");
    sheet.setAttribute("role", "dialog");
    sheet.setAttribute("aria-modal", "true");
    sheet.setAttribute("aria-labelledby", "pwa-sheet-title");
    sheet.innerHTML = '<div class="pwa-sheet-back" data-pwa-later></div><div class="pwa-sheet-card">' +
      '<button type="button" class="pwa-sheet-x" data-pwa-later aria-label="Close">&times;</button>' +
      '<div class="pwa-sheet-head">' + (info.icon ? '<img src="' + info.icon + '" alt="">' : "") +
      '<div><h2 id="pwa-sheet-title"></h2><p>অ্যাপ হিসেবে ফোনে ইন্সটল করুন</p></div></div>' +
      body + '<div class="pwa-sheet-acts">' + acts + '</div>' +
      (mode === "ios" ? '<span class="pwa-sheet-arrow" aria-hidden="true"></span>' : "") + '</div>';
    sheet.querySelector("h2").textContent = info.name;
    document.body.appendChild(sheet);
    requestAnimationFrame(function () { requestAnimationFrame(function () { if (sheet) sheet.classList.add("in"); }); });
    sheet.addEventListener("click", function (e) {
      if (e.target.closest("[data-pwa-later]")) { remember(); closeSheet(); }
      else if (e.target.closest("[data-pwa-ok]")) { remember(); closeSheet(); }
      else if (e.target.closest("[data-pwa-go]")) { install(); }
    });
  }
  window.pwaInstallSheet = showSheet;          /* for a quick look: pwaInstallSheet("android") / ("ios") */
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && sheet) { remember(); closeSheet(); } });

  function install() {
    if (!deferred) { closeSheet(); return; }
    var d = deferred;
    d.prompt();
    d.userChoice.then(function (r) {
      if (r && r.outcome === "dismissed") remember();
    }).finally(function () {
      deferred = null;
      closeSheet();
      document.querySelectorAll("[data-pwa-install]").forEach(function (x) { x.hidden = true; });
    });
  }
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    document.querySelectorAll("[data-pwa-install]").forEach(function (b) { b.hidden = false; });
    if (phone && !later()) setTimeout(function () { showSheet("android"); }, 2500);
  });
  if (ios && phone && !standalone && !later()) setTimeout(function () { showSheet("ios"); }, 3000);
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-pwa-install]");
    if (b) install();
  });
  window.addEventListener("appinstalled", function () {
    remember(3650 * 24 * 3600 * 1000);
    closeSheet();
    document.querySelectorAll("[data-pwa-install]").forEach(function (x) { x.hidden = true; });
  });
})();
