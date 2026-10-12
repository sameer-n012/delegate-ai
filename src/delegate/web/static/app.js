// Small glue around htmx: project switcher, drawer open/close, toasts, and live updates (SSE).
(function () {
  "use strict";

  var drawer = function () { return document.getElementById("drawer"); };
  var boardPath = function () { return "/p/" + document.body.dataset.project; };

  // The board URL keeps the column toggles (show_done, show_cancelled) as query parameters.
  function boardUrl() {
    var params = new URLSearchParams();
    document.querySelectorAll(".board-opt:checked").forEach(function (o) { params.set(o.name, o.value); });
    var q = params.toString();
    return boardPath() + (q ? "?" + q : "");
  }

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
    if (t.classList && t.classList.contains("board-opt")) {
      // Refresh only the board; the drawer and the URL path stay as they are.
      if (location.pathname === boardPath()) history.replaceState({}, "", boardUrl());
      htmx.trigger(document.body, "boardOpts");
    }
  });

  // A click outside the open drawer closes it. Clicks on links that load a task into the
  // drawer (cards, task links) switch the task instead, and toasts stay clickable. The
  // pointerdown check stops a text selection that starts in the drawer from closing it.
  var downInDrawer = false;
  document.addEventListener("pointerdown", function (e) {
    var d = drawer();
    downInDrawer = !!d && d.contains(e.target);
  });
  document.addEventListener("click", function (e) {
    if (e.target.id === "drawer-close") { closeDrawer(); return; }
    var d = drawer();
    if (!currentRef() || downInDrawer || d.contains(e.target)) return;
    if (!document.contains(e.target)) return; // removed by a swap during the click
    if (e.target.closest('[hx-target="#drawer-body"], #toasts')) return;
    closeDrawer();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && currentRef()) closeDrawer();
  });
  function closeDrawer() {
    openDrawer(false);
    history.pushState({}, "", boardUrl());
  }
  window.addEventListener("popstate", function () {
    if (location.pathname === boardPath()) openDrawer(false);
  });

  // Ask before a project is deleted. A static hx-confirm would also ask for the other states.
  document.body.addEventListener("htmx:confirm", function (e) {
    var form = e.detail.elt;
    if (!form.classList || !form.classList.contains("stateform")) return;
    var pick = form.elements.state;
    if (!pick || pick.value !== "deleted" || form.dataset.current === "deleted") return;
    e.preventDefault();
    if (window.confirm("Delete this project? It is hidden from the project list but can be restored.")) {
      e.detail.issueRequest(true);
    } else {
      pick.value = form.dataset.current; // put the picker back
    }
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
