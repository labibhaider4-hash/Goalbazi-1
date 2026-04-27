/* nav.js — shared navbar logic for all pages */

const GoalbaziTheme = {
  storageKey: "goalbazi-theme",
  apply(theme) {
    const next = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem(this.storageKey, next); } catch {}
    this.syncButtons();
  },
  current() {
    return document.documentElement.getAttribute("data-theme") || "dark";
  },
  init() {
    try {
      const saved = localStorage.getItem(this.storageKey);
      if (saved) this.apply(saved);
      else this.syncButtons();
    } catch {
      this.syncButtons();
    }
  },
  toggle() {
    this.apply(this.current() === "light" ? "dark" : "light");
  },
  syncButtons() {
    const isLight = this.current() === "light";
    document.querySelectorAll("[data-theme-toggle]").forEach(btn => {
      btn.innerHTML = `
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
          <path d="M9 18h6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
          <path d="M10 21h4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
          <path d="M12 3a6.5 6.5 0 0 0-3.86 11.73c.55.42.86 1.05.86 1.75V17h6v-.52c0-.7.31-1.33.86-1.75A6.5 6.5 0 0 0 12 3Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
        </svg>
      `;
      btn.title = isLight ? "Switch to dark theme" : "Switch to light theme";
      btn.setAttribute("aria-label", btn.title);
    });
  },
  attachButton(button) {
    if (!button || button.dataset.themeBound === "1") return;
    button.dataset.themeBound = "1";
    button.addEventListener("click", () => this.toggle());
    this.syncButtons();
  }
};

GoalbaziTheme.init();
window.GoalbaziTheme = GoalbaziTheme;

function initNav(activePage) {
  const pages = [
    { id: "dashboard", label: "Dashboard", href: "/dashboard" },
    { id: "games",     label: "Games",     href: "/games" },
    { id: "turfs",     label: "Arenas",    href: "/turfs" },
    { id: "leagues",   label: "Leagues",   href: "/leagues" },
    { id: "profile",   label: "Profile",   href: "/profile" },
  ];

  const navbar = document.getElementById("navbar");
  if (!navbar) return;

  const linksHtml = pages.map(p => `
    <a href="${p.href}" class="nav-link ${activePage === p.id ? "active" : ""}">${p.label}</a>
  `).join("");

  const drawerLinksHtml = pages.map(p => `
    <a href="${p.href}" class="nav-link ${activePage === p.id ? "active" : ""}">${p.label}</a>
  `).join("");

  navbar.innerHTML = `
    <a href="/dashboard" class="nav-brand">
      <img src="/assets/goalbazi-logo.svg" alt="Goalbazi">
      Goalbazi
    </a>
    <nav class="nav-links">${linksHtml}</nav>
    <div class="nav-right">
      <button class="theme-toggle hide-mobile" id="nav-theme-toggle" type="button" data-theme-toggle></button>
      <div class="nav-avatar" id="nav-avatar" title="Profile">?</div>
      <button class="btn btn-ghost btn-sm hide-mobile" id="nav-logout">Log out</button>
    </div>
    <button class="nav-hamburger" id="nav-hamburger" aria-label="Menu">
      <span></span><span></span><span></span>
    </button>
  `;

  // Drawer
  const drawer = document.getElementById("nav-drawer");
  if (drawer) {
    drawer.innerHTML = `
      ${drawerLinksHtml}
      <div class="nav-drawer-bottom">
        <button class="theme-toggle btn-full" id="nav-theme-toggle-mobile" type="button" data-theme-toggle></button>
        <button class="btn btn-ghost btn-sm btn-full" id="nav-logout-mobile">Log out</button>
      </div>
    `;
  }

  GoalbaziTheme.attachButton(document.getElementById("nav-theme-toggle"));
  GoalbaziTheme.attachButton(document.getElementById("nav-theme-toggle-mobile"));

  // Load avatar initials
  fetch("/api/auth/me").then(r => r.ok ? r.json() : null).then(user => {
    if (!user) return;
    const initials = user.name.split(" ").map(p => p[0]).slice(0, 2).join("").toUpperCase();
    const el = document.getElementById("nav-avatar");
    if (el) { el.textContent = initials; el.title = user.name; }
  });

  // Hamburger toggle
  const hamburger = document.getElementById("nav-hamburger");
  if (hamburger && drawer) {
    hamburger.addEventListener("click", () => drawer.classList.toggle("open"));
    document.addEventListener("click", e => {
      if (!navbar.contains(e.target) && !drawer.contains(e.target)) {
        drawer.classList.remove("open");
      }
    });
  }

  // Logout
  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.href = "/";
  }

  const logoutBtn = document.getElementById("nav-logout");
  const logoutMobile = document.getElementById("nav-logout-mobile");
  if (logoutBtn) logoutBtn.addEventListener("click", logout);
  if (logoutMobile) logoutMobile.addEventListener("click", logout);
}

function showToast(message, duration = 2400) {
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => toast.classList.remove("show"), duration);
}

async function apiFetch(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 401) { window.location.href = "/login"; return null; }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || "Request failed");
  }
  return res.json();
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  });
}
