// Dark mode toggle, shared by index.html and dashboard.html. The theme
// itself is already applied before this file loads (see the small inline
// script in each page's <head>, which avoids a flash of the wrong theme on
// load) -- this file just wires up the button and keeps things in sync.

(function () {
  const button = document.querySelector("#themeToggleButton");
  if (!button) {
    return;
  }

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  function renderButton(theme) {
    // Shows what clicking it would switch TO, not the current state --
    // matches how a light switch is usually labeled.
    button.textContent = theme === "dark" ? "☀️" : "🌙";
    button.title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
  }

  renderButton(currentTheme());

  button.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("theme", next);
    } catch (error) {
      // Storage blocked (e.g. private browsing) -- theme still applies for
      // this page view, it just won't be remembered next time.
    }
    renderButton(next);
    // Lets any page-specific code (e.g. the dashboard's hand-drawn SVG
    // charts, which bake in a few colors at render time) redraw itself
    // without a full reload.
    window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: next } }));
  });
})();


// "The app was updated" banner, shared by every page. The server's version
// changes whenever the program or these pages change; a page left open from
// before an update would otherwise keep showing the old buttons and screens.
(function () {
  const CHECK_MS = 30000;
  let loadedVersion = null;
  let shown = false;

  async function check() {
    if (shown) return;
    try {
      const response = await fetch("/api/version", { cache: "no-store" });
      if (!response.ok) return;
      const { version } = await response.json();
      if (loadedVersion === null) {
        loadedVersion = version;
      } else if (version !== loadedVersion) {
        showBanner();
      }
    } catch (error) {
      // Server restarting or unreachable: try again next time.
    }
  }

  function showBanner() {
    shown = true;
    const banner = document.createElement("div");
    banner.className = "update-banner";
    banner.setAttribute("role", "status");
    banner.innerHTML = '<span>The app was updated.</span> <button type="button">Reload to see the changes</button>';
    banner.querySelector("button").addEventListener("click", () => window.location.reload());
    document.body.prepend(banner);
  }

  check();
  setInterval(check, CHECK_MS);
})();
