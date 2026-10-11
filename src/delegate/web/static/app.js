// Small glue around htmx: project switcher, drawer open/close, toasts, and live updates (SSE).
(function () {
  "use strict";

  var drawer = function () { return document.getElementById("drawer"); };
  var boardUrl = function () { return "/p/" + document.body.dataset.project; };

  function openDrawer(open) {
    var d = drawer();
    if (!d) return;
    if (open) d.setAttribute("data-open", "true");
    else d.removeAttribute("data-open");
  }

  function currentRef() {
    var el = document.querySelector("#drawer-body .detail");
    return el && drawer().getAttribute("data-open") === "true" ? el.dataset.ref : null;
  }

  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t.id === "project-switch") location.href = t.value ? "/p/" + t.value : "/";
    if (t.id === "show-cancelled") location.href = boardUrl() + (t.checked ? "?show_cancelled=1" : "");
  });

  document.addEventListener("click", function (e) {
    if (e.target.id === "drawer-close") closeDrawer();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && currentRef()) closeDrawer();
  });
  function closeDrawer() {
    openDrawer(false);
    history.pushState({}, "", boardUrl());
  }
  window.addEventListener("popstate", function () {
    if (location.pathname === boardUrl()) openDrawer(false);
  });

  // htmx drops 4xx bodies by default. Our error handler returns a toast fragment, so allow the swap.
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var s = e.detail.xhr.status;
    if (s >= 400 && s < 500) { e.detail.shouldSwap = true; e.detail.isError = false; }
  });
  document.body.addEventListener("htmx:afterSwap", function (e) {
    var t = e.detail.target;
    if (t && t.id === "drawer-body") openDrawer(true);
    if (t && t.id === "toasts") {
      var last = t.lastElementChild;
      if (last) setTimeout(function () { last.remove(); }, 7000);
    }
  });
  document.body.addEventListener("htmx:sendError", function () {
    var t = document.getElementById("toasts");
    if (t) t.insertAdjacentHTML("beforeend", '<div class="toast err" role="alert"><b>offline</b> Cannot reach the dashboard server.</div>');
  });

  // Live updates: one EventSource per board page. Bursts collapse into one refresh (300 ms).
  var layout = document.querySelector(".layout[data-stream]");
  if (layout && window.EventSource) {
    var ind = document.getElementById("live");
    var label = ind && ind.querySelector("span:last-child");
    var timer = null, touched = {};
    var src = new EventSource(layout.dataset.stream);
    src.onopen = function () { if (ind) { ind.className = "live-ind on"; label.textContent = "live"; } };
    src.onerror = function () { if (ind) { ind.className = "live-ind off"; label.textContent = "reconnecting"; } };
    src.addEventListener("delegate", function (e) {
      var ev = JSON.parse(e.data);
      if (ev.task) touched[ev.task] = true;
      clearTimeout(timer);
      timer = setTimeout(refresh, 300);
    });
    function refresh() {
      htmx.trigger(document.body, "boardChanged");
      var ref = currentRef();
      var body = document.querySelector("#drawer-body .detail");
      // Skip the drawer refresh while the user types in it, so drafts are not lost.
      var typing = body && body.contains(document.activeElement) &&
        /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (ref && touched[ref] && !typing) {
        htmx.ajax("GET", body.dataset.url, { target: "#drawer-body", swap: "innerHTML" });
      }
      touched = {};
    }
  }
})();
