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

const GoalbaziInstall = {
  promptEvent: null,
  dismissedKey: "goalbazi-install-dismissed",
  isInstalled() {
    return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  },
  isDismissed() {
    try { return localStorage.getItem(this.dismissedKey) === "1"; } catch { return false; }
  },
  updateButtons() {
    const canInstall = Boolean(this.promptEvent) && !this.isInstalled();
    document.querySelectorAll("[data-install-app]").forEach(btn => {
      btn.hidden = !canInstall;
    });
    document.querySelectorAll("[data-install-banner]").forEach(banner => {
      banner.hidden = !canInstall || this.isDismissed();
    });
  },
  ensureBanner() {
    if (document.getElementById("install-app-banner")) return;
    const banner = document.createElement("div");
    banner.id = "install-app-banner";
    banner.className = "install-app-banner";
    banner.setAttribute("data-install-banner", "");
    banner.hidden = true;
    banner.innerHTML = `
      <div class="install-app-copy">
        <strong>Install Goalbazi</strong>
        <span>Open it faster from your phone home screen.</span>
      </div>
      <button class="btn btn-primary btn-sm" type="button" data-install-app>Install</button>
      <button class="install-app-close" type="button" data-install-dismiss aria-label="Close install prompt">x</button>
    `;
    document.body.appendChild(banner);
  },
  async install() {
    if (!this.promptEvent) {
      showToast("Use your browser menu and choose Add to Home Screen.");
      return;
    }
    this.promptEvent.prompt();
    await this.promptEvent.userChoice;
    this.promptEvent = null;
    this.updateButtons();
  },
  bind() {
    this.ensureBanner();
    document.querySelectorAll("[data-install-app]").forEach(btn => {
      if (btn.dataset.installBound === "1") return;
      btn.dataset.installBound = "1";
      btn.addEventListener("click", () => this.install());
    });
    document.querySelectorAll("[data-install-dismiss]").forEach(btn => {
      if (btn.dataset.dismissBound === "1") return;
      btn.dataset.dismissBound = "1";
      btn.addEventListener("click", () => {
        try { localStorage.setItem(this.dismissedKey, "1"); } catch {}
        this.updateButtons();
      });
    });
    this.updateButtons();
  }
};

window.addEventListener("beforeinstallprompt", event => {
  event.preventDefault();
  GoalbaziInstall.promptEvent = event;
  GoalbaziInstall.updateButtons();
});

window.addEventListener("appinstalled", () => {
  GoalbaziInstall.promptEvent = null;
  GoalbaziInstall.updateButtons();
  showToast("Goalbazi installed");
});

window.GoalbaziInstall = GoalbaziInstall;

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
      <div class="nav-profile-wrap">
        <button class="nav-avatar" id="nav-avatar" type="button" title="Profile" aria-label="Open profile menu">?</button>
        <div class="nav-profile-menu" id="nav-profile-menu" hidden>
          <a href="/profile">Profile</a>
          <a href="/admin" id="nav-admin-link" hidden>Admin Panel</a>
          <button type="button" id="nav-menu-logout">Log out</button>
        </div>
      </div>
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
        <a href="/admin" class="btn btn-ghost btn-sm btn-full" id="nav-admin-link-mobile" hidden>Admin Panel</a>
        <button class="btn btn-primary btn-sm btn-full install-app-btn" id="nav-install-mobile" type="button" data-install-app hidden>Install app</button>
        <button class="theme-toggle btn-full" id="nav-theme-toggle-mobile" type="button" data-theme-toggle></button>
        <button class="btn btn-ghost btn-sm btn-full" id="nav-logout-mobile">Log out</button>
      </div>
    `;
  }

  GoalbaziTheme.attachButton(document.getElementById("nav-theme-toggle"));
  GoalbaziTheme.attachButton(document.getElementById("nav-theme-toggle-mobile"));
  GoalbaziInstall.bind();

  // Load avatar initials
  fetch("/api/auth/me").then(r => r.ok ? r.json() : null).then(user => {
    if (!user) return;
    const initials = user.name.split(" ").map(p => p[0]).slice(0, 2).join("").toUpperCase();
    const el = document.getElementById("nav-avatar");
    if (el) {
      el.innerHTML = user.avatar_base64 ? `<img src="${user.avatar_base64}" alt="${user.name}">` : initials;
      el.title = user.name;
    }
    const adminLink = document.getElementById("nav-admin-link");
    if (adminLink) {
      adminLink.hidden = false;
      adminLink.style.display = user.is_admin ? "flex" : "none";
    }
    const adminLinkMobile = document.getElementById("nav-admin-link-mobile");
    if (adminLinkMobile) {
      adminLinkMobile.hidden = false;
      adminLinkMobile.style.display = user.is_admin ? "inline-flex" : "none";
    }
  });

  const avatarBtn = document.getElementById("nav-avatar");
  const profileMenu = document.getElementById("nav-profile-menu");
  if (avatarBtn && profileMenu) {
    avatarBtn.addEventListener("click", event => {
      event.stopPropagation();
      profileMenu.hidden = !profileMenu.hidden;
    });
    document.addEventListener("click", event => {
      if (!profileMenu.hidden && !profileMenu.contains(event.target) && event.target !== avatarBtn) {
        profileMenu.hidden = true;
      }
    });
  }

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
  const logoutMenu = document.getElementById("nav-menu-logout");
  if (logoutBtn) logoutBtn.addEventListener("click", logout);
  if (logoutMobile) logoutMobile.addEventListener("click", logout);
  if (logoutMenu) logoutMenu.addEventListener("click", logout);
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
