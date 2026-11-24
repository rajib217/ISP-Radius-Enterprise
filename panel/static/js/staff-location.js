/* Technician location sharing (mod_staffpush.staff_location): while the panel is open on the technician's phone, send
   its position when it moved about 40 m or every 3 minutes (never more than once in 30 s). Phones do not let a web app
   follow anyone in the background, so nothing is sent while the app is closed. */
(function () {
  var me = document.currentScript;
  if (!me || !("geolocation" in navigator) || !window.isSecureContext) return;
  var url = me.dataset.url, csrf = me.dataset.csrf || "", last = null, lastAt = 0, sending = false;
  var status = function (t) { var s = document.querySelector("[data-loc-status]"); if (s) s.textContent = t; };
  function metres(a, b) {
    var r = 6371000, p1 = a.lat * Math.PI / 180, p2 = b.lat * Math.PI / 180, dp = p2 - p1, dl = (b.lng - a.lng) * Math.PI / 180;
    var h = Math.sin(dp / 2) * Math.sin(dp / 2) + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * r * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function send(pos) {
    var now = Date.now(), here = { lat: pos.coords.latitude, lng: pos.coords.longitude };
    if (sending || now - lastAt < 30000) return;
    if (last && metres(last, here) < 40 && now - lastAt < 180000) return;
    sending = true;
    fetch(url, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
                 body: JSON.stringify({ lat: here.lat, lng: here.lng, acc: Math.round(pos.coords.accuracy || 0) }) })
      .then(function (r) { if (r.ok) { last = here; lastAt = now; status("Last sent just now."); } })
      .catch(function () {}).then(function () { sending = false; });
  }
  navigator.geolocation.watchPosition(send, function (err) {
    if (err && err.code === 1) status("Location is blocked on this phone, so the office cannot see where you are.");
  }, { enableHighAccuracy: true, maximumAge: 60000, timeout: 30000 });
  // a phone that stands still gives no new fix: ask again every 3 minutes so the office still sees it is live
  setInterval(function () {
    if (!document.hidden) navigator.geolocation.getCurrentPosition(send, function () {}, { maximumAge: 120000, timeout: 30000 });
  }, 180000);
})();
