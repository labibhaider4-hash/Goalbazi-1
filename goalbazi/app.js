const state = {
  profile: null,
  stats: [],
  games: [],
  turfs: [],
  leagues: [],
  standings: [],
  selectedGameId: null,
  selectedDate: null
};

const gameFormats = ["5v5", "7v7", "11v11"];
const skillLevels = ["Beginner", "Intermediate", "Competitive"];

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || "Request failed");
  }
  return response.json();
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 2200);
}

function initialsFromName(name) {
  return name.split(" ").map(part => part[0]).slice(0, 2).join("").toUpperCase();
}

function formatDate(dateText) {
  const date = new Date(dateText);
  return new Intl.DateTimeFormat("en-IN", { weekday: "short", month: "short", day: "numeric" }).format(date);
}

function formatCountdown(isoDateTime) {
  const diff = new Date(isoDateTime).getTime() - Date.now();
  if (diff <= 0) return "00:00:00";
  const total = Math.floor(diff / 1000);
  const hours = String(Math.floor(total / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const seconds = String(total % 60).padStart(2, "0");
  return `${hours}:${minutes}:${seconds}`;
}

function renderStats() {
  document.getElementById("stats-grid").innerHTML = state.stats.map(item => `
    <div class="stat-card">
      <div class="stat-value">${item.value}</div>
      <div class="stat-label">${item.label}</div>
    </div>
  `).join("");
}

function renderProfile() {
  if (!state.profile) return;
  document.getElementById("profile-avatar").textContent = initialsFromName(state.profile.name);
  document.getElementById("profile-name").textContent = state.profile.name;
  document.getElementById("profile-meta").textContent = `${state.profile.handle} / ${state.profile.location}`;
  document.getElementById("profile-tags").innerHTML = [
    state.profile.position,
    state.profile.preferred_format,
    state.profile.skill
  ].filter(Boolean).map(tag => `<span class="tag">${tag}</span>`).join("");

  document.getElementById("profile-name-input").value = state.profile.name;
  document.getElementById("profile-handle-input").value = state.profile.handle;
  document.getElementById("profile-location-input").value = state.profile.location;
  document.getElementById("profile-format-input").value = state.profile.preferred_format;
  document.getElementById("profile-bio-input").value = state.profile.bio ?? "";
}

function renderGames() {
  document.getElementById("games-region").textContent = state.profile ? state.profile.location : "";
  document.getElementById("games-list").innerHTML = state.games.map(game => `
    <button class="card game-card" data-game-id="${game.id}">
      <div>
        <div class="card-title">${game.title}</div>
        <div class="card-meta">${formatDate(game.game_date)} / ${game.game_time} / ${game.location}</div>
        <div class="card-sub">${game.skill_level} / ${game.format} / ${game.confirmed_players} confirmed</div>
      </div>
      <span class="pill ${game.status.toLowerCase()}">${game.status}</span>
    </button>
  `).join("");
}

function renderLobby() {
  const game = state.games.find(item => item.id === state.selectedGameId);
  if (!game) {
    document.getElementById("lobby-title").textContent = "No game selected";
    document.getElementById("lobby-meta").textContent = "Choose a game from the list to load its lobby.";
    document.getElementById("lobby-status").textContent = "Choose a game";
    document.getElementById("team-a-list").innerHTML = "";
    document.getElementById("team-b-list").innerHTML = "";
    document.getElementById("chat-feed").innerHTML = "";
    return;
  }

  document.getElementById("lobby-title").textContent = game.title;
  document.getElementById("lobby-meta").textContent = `${formatDate(game.game_date)} / ${game.game_time} / ${game.location} / ${game.format}`;
  document.getElementById("lobby-status").textContent = game.status;
  document.getElementById("lobby-countdown").textContent = formatCountdown(game.kickoff_at);

  const teamA = game.players.filter(player => player.team_name === "A");
  const teamB = game.players.filter(player => player.team_name === "B");
  document.getElementById("team-a-list").innerHTML = renderTeamSlots(teamA, game.players_per_team);
  document.getElementById("team-b-list").innerHTML = renderTeamSlots(teamB, game.players_per_team);

  document.getElementById("chat-feed").innerHTML = game.messages.map(message => {
    if (message.is_system) {
      return `<div class="chat-system">${message.message}</div>`;
    }
    return `
      <div class="chat-item">
        <div class="chat-sender">${message.sender_name}</div>
        <div class="chat-text">${message.message}</div>
      </div>
    `;
  }).join("");
}

function renderTeamSlots(players, maxPlayers) {
  const cards = [...players];
  while (cards.length < maxPlayers) {
    cards.push(null);
  }
  return cards.map(player => {
    if (!player) {
      return `
        <div class="slot-card">
          <div class="slot-name slot-open">Open slot</div>
          <div class="slot-role">Waiting for a player</div>
        </div>
      `;
    }
    return `
      <div class="slot-card">
        <div class="slot-name">${player.player_name}${player.is_captain ? " / Captain" : ""}</div>
        <div class="slot-role">${player.player_role}</div>
      </div>
    `;
  }).join("");
}

function renderTurfs() {
  document.getElementById("turf-summary").textContent = `${state.turfs.length} turfs available`;
  document.getElementById("game-turf-select").innerHTML = state.turfs.map(turf => `
    <option value="${turf.id}">${turf.name} / ${turf.area}</option>
  `).join("");
  document.getElementById("turfs-list").innerHTML = state.turfs.map(turf => `
    <div class="card">
      <div>
        <div class="card-title">${turf.name}</div>
        <div class="card-meta">${turf.area} / ${turf.distance_km} km / ${turf.surface}</div>
        <div class="card-sub">Rating ${turf.rating} / Rs ${turf.price_per_hour} per hour</div>
        <div class="turf-slots">
          ${turf.slots.map(slot => `
            <button
              class="slot-btn ${slot.is_booked ? "booked" : "available"}"
              data-slot-id="${slot.id}"
              ${slot.is_booked ? "disabled" : ""}
            >
              ${slot.slot_time}
            </button>
          `).join("")}
        </div>
      </div>
      <span class="pill available">${turf.surface}</span>
    </div>
  `).join("");
}

function renderLeagues() {
  document.getElementById("league-grid").innerHTML = state.leagues.map(league => `
    <div class="league-card">
      <div class="league-name">${league.name}</div>
      <div class="league-sub">${league.description}</div>
      <div class="league-stats">
        <span>${league.format}</span>
        <span>${league.stage}</span>
        <span>${league.status}</span>
      </div>
    </div>
  `).join("");

  document.getElementById("standings-body").innerHTML = state.standings.map(row => `
    <tr>
      <td>${row.rank}</td>
      <td>${row.team_name}</td>
      <td>${row.played}</td>
      <td>${row.won}</td>
      <td>${row.points}</td>
      <td>
        <div class="form-dots">
          ${row.form.split(",").map(item => `<span class="form-dot ${item === "W" ? "form-win" : item === "D" ? "form-draw" : "form-loss"}"></span>`).join("")}
        </div>
      </td>
    </tr>
  `).join("");
}

async function loadDashboard() {
  const date = document.getElementById("turf-date").value;
  const search = document.getElementById("turf-search").value.trim();
  const query = new URLSearchParams();
  if (date) query.set("date", date);
  if (search) query.set("search", search);

  const data = await request(`/api/dashboard?${query.toString()}`);
  state.profile = data.profile;
  state.stats = data.stats;
  state.games = data.games;
  state.turfs = data.turfs;
  state.leagues = data.leagues;
  state.standings = data.standings;

  if (!state.selectedGameId && state.games[0]) {
    state.selectedGameId = state.games[0].id;
  } else if (state.selectedGameId && !state.games.find(game => game.id === state.selectedGameId)) {
    state.selectedGameId = state.games[0]?.id ?? null;
  }

  renderStats();
  renderProfile();
  renderGames();
  renderTurfs();
  renderLeagues();
  renderLobby();
}

async function updateLobbyGame(gameId) {
  const game = await request(`/api/games/${gameId}`);
  state.selectedGameId = game.id;
  const index = state.games.findIndex(item => item.id === game.id);
  if (index >= 0) {
    state.games[index] = game;
  } else {
    state.games.unshift(game);
  }
  renderGames();
  renderLobby();
}

function setupStaticInputs() {
  document.getElementById("game-format-select").innerHTML = gameFormats.map(item => `<option value="${item}">${item}</option>`).join("");
  document.getElementById("game-skill-select").innerHTML = skillLevels.map(item => `<option value="${item}">${item}</option>`).join("");
  const today = new Date();
  const iso = today.toISOString().slice(0, 10);
  document.getElementById("game-date-input").value = iso;
  document.getElementById("turf-date").value = iso;
}

function attachEvents() {
  document.addEventListener("click", async event => {
    const jump = event.target.closest("[data-jump]");
    if (jump) {
      document.getElementById(jump.dataset.jump).scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    const gameCard = event.target.closest("[data-game-id]");
    if (gameCard) {
      await updateLobbyGame(gameCard.dataset.gameId);
      document.getElementById("lobby").scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    const slotButton = event.target.closest("[data-slot-id]");
    if (slotButton && !slotButton.disabled) {
      await request(`/api/bookings/${slotButton.dataset.slotId}`, { method: "POST" });
      showToast("Turf slot booked");
      await loadDashboard();
    }
  });

  document.getElementById("profile-form").addEventListener("submit", async event => {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    await request("/api/profile", {
      method: "PUT",
      body: JSON.stringify(Object.fromEntries(formData.entries()))
    });
    showToast("Profile updated");
    await loadDashboard();
  });

  document.getElementById("game-form").addEventListener("submit", async event => {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    const payload = Object.fromEntries(formData.entries());
    const result = await request("/api/games", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    showToast("Game created");
    await loadDashboard();
    await updateLobbyGame(result.id);
    document.getElementById("lobby").scrollIntoView({ behavior: "smooth", block: "start" });
  });

  document.getElementById("chat-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!state.selectedGameId) return;
    const input = document.getElementById("chat-input");
    const message = input.value.trim();
    if (!message) return;
    await request(`/api/games/${state.selectedGameId}/messages`, {
      method: "POST",
      body: JSON.stringify({ message })
    });
    input.value = "";
    await updateLobbyGame(state.selectedGameId);
  });

  document.getElementById("confirm-btn").addEventListener("click", async () => {
    if (!state.selectedGameId) return;
    await request(`/api/games/${state.selectedGameId}/attendance`, { method: "POST" });
    showToast("Attendance confirmed");
    await updateLobbyGame(state.selectedGameId);
  });

  document.getElementById("leave-btn").addEventListener("click", async () => {
    if (!state.selectedGameId) return;
    await request(`/api/games/${state.selectedGameId}/attendance`, { method: "DELETE" });
    showToast("You left the game");
    await updateLobbyGame(state.selectedGameId);
  });

  document.getElementById("turf-search").addEventListener("input", loadDashboard);
  document.getElementById("turf-date").addEventListener("change", loadDashboard);
  document.getElementById("refresh-profile-btn").addEventListener("click", loadDashboard);
}

function startLobbyTimer() {
  setInterval(() => {
    if (!state.selectedGameId) return;
    const game = state.games.find(item => item.id === state.selectedGameId);
    if (game) {
      document.getElementById("lobby-countdown").textContent = formatCountdown(game.kickoff_at);
    }
  }, 1000);
}

async function init() {
  setupStaticInputs();
  attachEvents();
  await loadDashboard();
  startLobbyTimer();
}

init().catch(error => {
  console.error(error);
  showToast("Something went wrong while loading the app");
});
