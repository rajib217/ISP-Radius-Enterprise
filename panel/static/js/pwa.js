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

  /* ---- install button ---- */
  var deferred = null;
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    document.querySelectorAll("[data-pwa-install]").forEach(function (b) { b.hidden = false; });
  });
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-pwa-install]");
    if (!b || !deferred) return;
    deferred.prompt();
    deferred.userChoice.finally(function () {
      deferred = null;
      document.querySelectorAll("[data-pwa-install]").forEach(function (x) { x.hidden = true; });
    });
  });
  window.addEventListener("appinstalled", function () {
    document.querySelectorAll("[data-pwa-install]").forEach(function (x) { x.hidden = true; });
  });
})();
