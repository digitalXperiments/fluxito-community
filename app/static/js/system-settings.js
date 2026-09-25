(function () {
  "use strict";

  function bumpSummary(name, delta) {
    var el = document.querySelector('[data-summary-value="' + name + '"]');
    if (!el) return;
    var current = parseInt(el.textContent || "0", 10);
    if (Number.isNaN(current)) return;
    el.textContent = String(Math.max(0, current + delta));
  }

  function save(event) {
    event.preventDefault();
    var form = event.target;
    var key = form.getAttribute("data-key");
    var input = form.elements.value;
    var value = input ? input.value : "";
    var saveBtn = form.querySelector('button[type="submit"]');
    var saveText = saveBtn ? saveBtn.querySelector("span") : null;
    if (saveBtn) {
      saveBtn.disabled = true;
      if (saveText) saveText.textContent = "Saving";
    }

    fetch("/api/settings/system/" + encodeURIComponent(key), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: value }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || "Save failed"); });
        return r.json();
      })
      .then(function () {
        if (form.getAttribute("data-secret") === "1" && input) {
          input.value = "";
          input.placeholder = "********";
        }
        var badge = form.querySelector(".sys-badge");
        if (badge) {
          var wasDb = (badge.textContent || "").trim().toLowerCase() === "db";
          badge.textContent = "db";
          badge.classList.add("is-db");
          badge.classList.remove("is-env");
          if (!wasDb) {
            bumpSummary("db", 1);
            bumpSummary("env", -1);
          }
        }
        form.classList.remove("is-dirty");
        Fluxito.toast("Setting saved");
      })
      .catch(function (err) {
        Fluxito.toast(err.message || "Save failed", "error");
      })
      .finally(function () {
        if (saveBtn) {
          saveBtn.disabled = false;
          if (saveText) saveText.textContent = "Save";
        }
      });
  }

  function reset(key) {
    if (!window.confirm("Reset this setting to its env/default fallback?")) return;
    fetch("/api/settings/system/" + encodeURIComponent(key), {
      method: "DELETE",
      credentials: "same-origin",
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || "Reset failed"); });
        return r.json();
      })
      .then(function () {
        Fluxito.toast("Setting reset");
        window.location.reload();
      })
      .catch(function (err) {
        Fluxito.toast(err.message || "Reset failed", "error");
      });
  }

  window.SystemSettings = {
    save: save,
    reset: reset,
  };

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".sys-row .sys-input").forEach(function (input) {
      input.addEventListener("input", function () {
        var form = input.closest(".sys-row");
        if (form) form.classList.add("is-dirty");
      });
    });
  });
})();
