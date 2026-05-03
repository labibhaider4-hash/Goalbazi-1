/* nav.js — shared navbar logic for all pages */

/* Feature map:
   - dark/light theme toggle
   - installable PWA prompt
   - installed-app update banner
   - phone push notification opt-in
   - shared navbar, profile avatar menu, admin shortcut, logout, toast, and API helper
*/

const GoalbaziTheme = {
  // Keeps the user's light/dark preference in localStorage and updates all theme buttons.
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
      const icon = `
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
          <path d="M9 18h6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
          <path d="M10 21h4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
          <path d="M12 3a6.5 6.5 0 0 0-3.86 11.73c.55.42.86 1.05.86 1.75V17h6v-.52c0-.7.31-1.33.86-1.75A6.5 6.5 0 0 0 12 3Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
        </svg>
      `;
      const iconTarget = btn.querySelector("[data-theme-icon]");
      const labelTarget = btn.querySelector("[data-theme-label]");
      if (iconTarget) iconTarget.innerHTML = icon;
      else btn.innerHTML = icon;
      if (labelTarget) labelTarget.textContent = isLight ? "Switch to dark theme" : "Switch to light theme";
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
  // Shows the "Install Goalbazi" banner when the browser says the app can be installed.
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

const GoalbaziUpdates = {
  // Displays "Update available" when a newly deployed service worker is waiting.
  refreshing: false,
  waitingWorker: null,
  ensureBanner() {
    if (document.getElementById("app-update-banner")) return;
    const banner = document.createElement("div");
    banner.id = "app-update-banner";
    banner.className = "install-app-banner";
    banner.hidden = true;
    banner.innerHTML = `
      <div class="install-app-copy">
        <strong>Update available</strong>
        <span>A new Goalbazi version is ready.</span>
      </div>
      <button class="btn btn-primary btn-sm" type="button" id="app-update-btn">Update</button>
      <button class="install-app-close" type="button" id="app-update-close" aria-label="Close update prompt">x</button>
    `;
    document.body.appendChild(banner);
    document.getElementById("app-update-btn").addEventListener("click", () => this.apply());
    document.getElementById("app-update-close").addEventListener("click", () => {
      banner.hidden = true;
    });
  },
  show(worker) {
    this.waitingWorker = worker;
    this.ensureBanner();
    const banner = document.getElementById("app-update-banner");
    if (banner) banner.hidden = false;
  },
  apply() {
    if (!this.waitingWorker) {
      window.location.reload();
      return;
    }
    this.waitingWorker.postMessage({ type: "SKIP_WAITING" });
  },
  async checkNow() {
    if (!("serviceWorker" in navigator)) {
      showToast("Updates are checked when you refresh this browser.");
      return;
    }
    showToast("Checking for updates...");
    try {
      const registration = await navigator.serviceWorker.getRegistration();
      if (!registration) {
        showToast("Update checker is not ready yet. Reopen the app once.");
        return;
      }
      await registration.update();
      if (registration.waiting) {
        this.show(registration.waiting);
        showToast("Update ready");
        return;
      }
      const installing = registration.installing;
      if (installing) {
        installing.addEventListener("statechange", () => {
          if (installing.state === "installed" && navigator.serviceWorker.controller) {
            this.show(installing);
          }
        });
        showToast("Preparing update...");
        return;
      }
      showToast("Goalbazi is already up to date");
    } catch {
      showToast("Could not check updates. Try again later.");
    }
  },
  bindRegistration(registration) {
    if (!registration) return;
    if (registration.waiting && navigator.serviceWorker.controller) {
      this.show(registration.waiting);
    }
    registration.addEventListener("updatefound", () => {
      const worker = registration.installing;
      if (!worker) return;
      worker.addEventListener("statechange", () => {
        if (worker.state === "installed" && navigator.serviceWorker.controller) {
          this.show(worker);
        }
      });
    });
  },
  bindControllerChange() {
    if (!("serviceWorker" in navigator) || this.controllerBound) return;
    this.controllerBound = true;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (this.refreshing) return;
      this.refreshing = true;
      window.location.reload();
    });
  }
};

window.GoalbaziUpdates = GoalbaziUpdates;

const GoalbaziPush = {
  // Handles phone notification permission and sends browser subscriptions to Flask.
  async publicKey() {
    const res = await fetch("/api/push/public-key");
    if (!res.ok) return null;
    return res.json();
  },
  urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const rawData = window.atob(base64);
    return Uint8Array.from([...rawData].map(char => char.charCodeAt(0)));
  },
  supported() {
    return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  },
  updateButtons() {
    const show = this.supported() && Notification.permission !== "granted";
    document.querySelectorAll("[data-enable-push]").forEach(btn => {
      btn.hidden = !show;
    });
  },
  async enable() {
    if (!this.supported()) {
      showToast("Phone notifications are not supported in this browser.");
      return;
    }
    const keyData = await this.publicKey();
    if (!keyData?.enabled || !keyData.publicKey) {
      showToast("Phone notifications are not configured yet.");
      return;
    }
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      showToast("Notifications were not allowed.");
      this.updateButtons();
      return;
    }
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: this.urlBase64ToUint8Array(keyData.publicKey),
    });
    await fetch("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(subscription),
    });
    showToast("Phone notifications enabled");
    this.updateButtons();
  },
  bind() {
    document.querySelectorAll("[data-enable-push]").forEach(btn => {
      if (btn.dataset.pushBound === "1") return;
      btn.dataset.pushBound = "1";
      btn.addEventListener("click", () => this.enable());
    });
    this.updateButtons();
  }
};

window.GoalbaziPush = GoalbaziPush;

const GoalbaziPullRefresh = {
  // Mobile-only branded pull-to-refresh. It appears only when pulling from page top.
  threshold: 78,
  maxPull: 104,
  startY: 0,
  pulling: false,
  refreshing: false,
  bound: false,
  frame: null,
  ensure() {
    if (document.getElementById("pull-refresh-indicator")) return;
    const indicator = document.createElement("div");
    indicator.id = "pull-refresh-indicator";
    indicator.className = "pull-refresh-indicator";
    indicator.setAttribute("aria-hidden", "true");
    indicator.innerHTML = `
      <span class="pull-refresh-ring"><span class="pull-refresh-logo"><img src="/assets/goalbazi-logo.svg" alt=""></span></span>
      <span class="pull-refresh-copy">SIUU</span>
    `;
    document.body.appendChild(indicator);
  },
  canStart(event) {
    return (
      event.touches &&
      event.touches.length === 1 &&
      window.scrollY <= 0 &&
      !this.refreshing &&
      !document.body.classList.contains("modal-open")
    );
  },
  setPull(distance) {
    const indicator = document.getElementById("pull-refresh-indicator");
    if (!indicator) return;
    const clamped = Math.max(0, Math.min(this.maxPull, distance));
    // v3.0: requestAnimationFrame keeps the SIUU pull animation smooth on phones.
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = requestAnimationFrame(() => {
      const eased = Math.round(clamped * 0.64);
      indicator.classList.toggle("visible", clamped > 8);
      indicator.classList.toggle("ready", clamped >= this.threshold);
      indicator.style.transform = `translate3d(-50%, ${-62 + eased}px, 0) scale(${0.96 + Math.min(clamped / 760, .06)})`;
    });
  },
  reset() {
    const indicator = document.getElementById("pull-refresh-indicator");
    if (!indicator) return;
    if (this.frame) cancelAnimationFrame(this.frame);
    indicator.classList.remove("visible", "ready");
    indicator.style.transform = "translate3d(-50%, -62px, 0) scale(.96)";
    document.body.classList.remove("pull-refreshing");
  },
  refresh() {
    const indicator = document.getElementById("pull-refresh-indicator");
    this.refreshing = true;
    document.body.classList.add("pull-refreshing");
    if (indicator) {
      indicator.classList.add("visible", "ready");
      indicator.style.transform = "translate3d(-50%, 8px, 0) scale(1)";
    }
    setTimeout(() => window.location.reload(), 420);
  },
  bind() {
    if (this.bound || !("ontouchstart" in window)) return;
    this.bound = true;
    this.ensure();
    document.addEventListener("touchstart", event => {
      if (!this.canStart(event)) return;
      this.startY = event.touches[0].clientY;
      this.pulling = true;
    }, { passive: true });
    document.addEventListener("touchmove", event => {
      if (!this.pulling) return;
      const distance = event.touches[0].clientY - this.startY;
      if (distance <= 0 || window.scrollY > 0) {
        this.pulling = false;
        this.reset();
        return;
      }
      if (distance > 12) event.preventDefault();
      this.setPull(distance);
    }, { passive: false });
    document.addEventListener("touchend", event => {
      if (!this.pulling) return;
      const changedTouch = event.changedTouches && event.changedTouches[0];
      const distance = changedTouch ? changedTouch.clientY - this.startY : 0;
      this.pulling = false;
      if (distance >= this.threshold) this.refresh();
      else this.reset();
    }, { passive: true });
    document.addEventListener("touchcancel", () => {
      this.pulling = false;
      this.reset();
    }, { passive: true });
  }
};

window.GoalbaziPullRefresh = GoalbaziPullRefresh;

const GoalbaziLoading = {
  // Shared top progress bar for API/page transitions; it stays subtle so pages never feel blocked.
  activeRequests: 0,
  hideTimer: null,
  ensure() {
    if (document.getElementById("goalbazi-top-loader")) return;
    const loader = document.createElement("div");
    loader.id = "goalbazi-top-loader";
    loader.className = "top-page-loader";
    loader.setAttribute("aria-hidden", "true");
    loader.innerHTML = `<span></span>`;
    document.body.appendChild(loader);
  },
  show() {
    this.ensure();
    clearTimeout(this.hideTimer);
    this.activeRequests += 1;
    document.getElementById("goalbazi-top-loader")?.classList.add("active");
  },
  hide() {
    this.activeRequests = Math.max(0, this.activeRequests - 1);
    if (this.activeRequests > 0) return;
    this.hideTimer = setTimeout(() => {
      document.getElementById("goalbazi-top-loader")?.classList.remove("active");
    }, 180);
  },
};

window.GoalbaziLoading = GoalbaziLoading;

function initNav(activePage) {
  // Renders the same navigation on every authenticated athlete page.
  const pages = [
    { id: "dashboard", label: "Dashboard", href: "/dashboard" },
    { id: "games",     label: "Games",     href: "/games" },
    { id: "turfs",     label: "Arenas",    href: "/turfs" },
    { id: "leagues",   label: "Leagues",   href: "/leagues" },
    { id: "profile",   label: "Profile",   href: "/profile" },
    { id: "about",     label: "About",     href: "/about" },
    { id: "support",   label: "Support",   href: "/support" },
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
          <button type="button" data-enable-push hidden>Enable phone notifications</button>
          <button type="button" data-check-updates>Check for updates</button>
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
        <button class="btn btn-ghost btn-sm btn-full" type="button" data-enable-push hidden>Enable phone notifications</button>
        <button class="btn btn-ghost btn-sm btn-full" type="button" data-check-updates>Check for updates</button>
        <button class="btn btn-primary btn-sm btn-full install-app-btn" id="nav-install-mobile" type="button" data-install-app hidden>Install app</button>
        <button class="btn btn-ghost btn-sm btn-full drawer-theme-toggle" id="nav-theme-toggle-mobile" type="button" data-theme-toggle>
          <span data-theme-icon></span>
          <span data-theme-label>Theme</span>
        </button>
        <button class="btn btn-ghost btn-sm btn-full" id="nav-logout-mobile">Log out</button>
      </div>
    `;
  }

  GoalbaziTheme.attachButton(document.getElementById("nav-theme-toggle"));
  GoalbaziTheme.attachButton(document.getElementById("nav-theme-toggle-mobile"));
  GoalbaziInstall.bind();
  GoalbaziPush.bind();
  GoalbaziPullRefresh.bind();
  document.querySelectorAll("[data-check-updates]").forEach(btn => {
    if (btn.dataset.updateBound === "1") return;
    btn.dataset.updateBound = "1";
    btn.addEventListener("click", () => GoalbaziUpdates.checkNow());
  });

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
    GoalbaziPush.updateButtons();
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

  // Show the slim loader immediately for normal same-site page transitions.
  document.querySelectorAll("a[href^='/']").forEach(link => {
    if (link.dataset.transitionBound === "1") return;
    link.dataset.transitionBound = "1";
    link.addEventListener("click", event => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.target) return;
      GoalbaziLoading.show();
    });
  });
}

function showToast(message, duration = 2400) {
  // Small shared notification helper for success/error messages.
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => toast.classList.remove("show"), duration);
}

async function apiFetch(path, options = {}) {
  // Shared fetch wrapper. It also drives the slim transition loader used across the app.
  GoalbaziLoading.show();
  try {
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
  } finally {
    GoalbaziLoading.hide();
  }
}

if ("serviceWorker" in navigator) {
  // Registers the PWA worker and wires app-update detection after page load.
  window.addEventListener("load", () => {
    GoalbaziPullRefresh.bind();
    GoalbaziUpdates.bindControllerChange();
    navigator.serviceWorker.register("/service-worker.js").then(registration => {
      GoalbaziUpdates.bindRegistration(registration);
      GoalbaziPush.updateButtons();
    }).catch(() => {});
  });
}
