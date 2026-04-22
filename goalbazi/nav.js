/* nav.js — shared navbar logic for all pages */

function initNav(activePage) {
  const pages = [
    { id: "dashboard", label: "Dashboard", href: "/dashboard" },
    { id: "games",     label: "Games",     href: "/games" },
    { id: "turfs",     label: "Turfs",     href: "/turfs" },
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
        <button class="btn btn-ghost btn-sm btn-full" id="nav-logout-mobile">Log out</button>
      </div>
    `;
  }

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
    window.location.href = "/login";
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
