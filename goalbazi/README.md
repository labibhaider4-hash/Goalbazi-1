# Goalbazi

Goalbazi is a mobile-first football community web app for athletes, arena partners, and admins. It helps players discover games, find arenas, manage profiles, message other athletes, follow leagues, and install the site as a phone web app.

This project is intentionally kept simple: a Flask backend, PostgreSQL database, HTML/CSS/JavaScript frontend files, and Railway deployment through Nixpacks. There is no Docker requirement.

## Main User Types

- **Athlete**: Signs in, updates profile, searches athletes, joins games, views arenas, follows leagues, messages friends, rates eligible players, and installs the PWA.
- **Arena Partner**: Registers an arena account, manages arena details, images, booking settings, UPI details, slots, and bookings.
- **Admin**: Has control over users, teams, arenas, leagues, archive/recovery flows, stats, player attributes, and platform data.

## Feature Overview

### Authentication

- Email/password login and registration for athletes.
- Separate Arena Partner login and registration.
- Admin access through `/admin` and the profile avatar dropdown when the logged-in user is an admin.
- Google login preparation exists in the UI, but Google OAuth still needs Google Cloud credentials before it becomes active.

### Dashboard

- Mobile-friendly dashboard cards for games, arenas, leagues, community, and profile access.
- Profile avatar shortcut in the top navigation.
- Profile picture appears when uploaded; initials are used only as a fallback.
- Goalbazi AI opens as a floating chat overlay instead of taking over the full page.
- App install prompt appears at the bottom and can be dismissed.
- App update banner appears when a newer PWA/service-worker version is available.

### Community And Messaging

- Athlete search and limited friend suggestions.
- Friend request flow.
- Private messaging between users.
- Notification UI for in-app updates.
- Phone push notification backend support through VAPID keys.

### Ratings

- Player ratings use a 1 to 10 scale.
- Players can rate only when they are friends or have played together.
- Rating identity is hidden from normal users.
- Admin can inspect rating details.

### Games

- Browse games.
- Create matches.
- Join/leave match lobbies.
- View team slots, attendance, chat, and countdown.
- Arena selection uses active arenas from the database.

### Arenas

- “Turf” wording has been replaced with more professional arena language in the user-facing experience.
- Arena Partner pages allow arena profile management, images, descriptions, slots, UPI QR/details, and booking settings.
- Athlete pages can search arenas near them or by city.

### Leagues

- Public league page shows only active leagues that have teams.
- Admin can create, edit, delete/archive, restore, and manage leagues.
- Teams and standings are connected to league data.
- Empty or inactive leagues are hidden from the public league page.

### Admin Panel

- Admin can manage players, player attributes, profile images, teams, arenas, leagues, stats, and related data.
- Team and arena delete flows are designed to persist instead of reappearing after reload.
- Archive system allows recovering mistakenly deleted data.
- Permanent delete option exists for archive cleanup.

### PWA / Web App

- `manifest.webmanifest` makes the site installable as a web app.
- `service-worker.js` caches key pages and assets.
- Installed app can receive update prompts after deployment changes.
- Push notifications require VAPID environment variables and browser permission.

### UI And Branding

- Dark/light theme toggle is available through the bulb-style button.
- Mobile layout has been prioritized because most users will use phones.
- Goalbazi logo is used across pages and browser tab icons.
- Arena Partner page has mobile-fit improvements and logo visibility.

## File Map

- `server.py`: Main Flask backend, database setup, auth routes, app routes, API routes, admin routes, arena partner routes, PWA push routes, and helper functions.
- `styles.css`: Shared design system, responsive layout, dark/light theme variables, cards, buttons, forms, navbar, and mobile styling.
- `nav.js`: Shared navbar behavior, theme toggle, install prompt, PWA update prompt, push notification setup, API helper, and toast messages.
- `service-worker.js`: PWA cache, update activation, offline fallback, push notification display, and notification click handling.
- `manifest.webmanifest`: Web app install metadata, colors, icons, and app shortcuts.
- `dashboard.html`: Main signed-in athlete dashboard.
- `games.html`: Browse and create games.
- `turfs.html`: Arena browsing and booking page.
- `leagues.html`: Public active league and standings page.
- `profile.html`: Athlete profile, avatar, stats, and profile editing.
- `admin.html`: Admin panel for platform management.
- `owner_dashboard.html`: Arena Partner dashboard.
- `owner_login.html`: Arena Partner login.
- `owner_register.html`: Arena Partner registration and first arena creation.
- `login.html`: Athlete login.
- `register.html`: Athlete registration.
- `landing.html`: Public landing page.
- `lobby.html`: Match lobby page.
- `index.html`: Legacy all-in-one dashboard reference page.
- `app.js`: Legacy single-page dashboard JavaScript.
- `seed_prod.py`: Manual database bootstrap helper.
- `requirements.txt`: Python dependencies.
- `railway.toml`: Railway Nixpacks build and deploy configuration.
- `Procfile`: Alternative process command for platforms that read Procfiles.
- `DEPLOY.html`: Visual deployment/launch instructions page.

## Local Setup

1. Install Python 3.12 or newer.
2. Open a terminal inside the `goalbazi` folder.
3. Create and activate a virtual environment.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

4. Install dependencies.

```powershell
pip install -r requirements.txt
```

5. Set required environment variables.

```powershell
$env:DATABASE_URL="your-postgres-url"
$env:SECRET_KEY="use-a-long-random-secret"
```

6. Start the app.

```powershell
python server.py
```

7. Open the site.

```text
http://127.0.0.1:8000
```

## Railway Deployment

Railway should use Nixpacks, not Docker.

The important deployment file is `railway.toml`:

```toml
[build]
builder = "NIXPACKS"

[deploy]
startCommand = "gunicorn server:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60"
healthcheckPath = "/health"
healthcheckTimeout = 120
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

Required Railway variables:

- `DATABASE_URL`: PostgreSQL connection URL from Railway Postgres.
- `SECRET_KEY`: Long random secret for Flask sessions.

Optional Railway variables:

- `ADMIN_EMAIL`: Email that should be treated as admin.
- `GOOGLE_CLIENT_ID`: Google OAuth client ID.
- `GOOGLE_CLIENT_SECRET`: Google OAuth secret.
- `OPENAI_API_KEY`: Enables stronger AI assistant replies if configured.
- `OPENAI_MODEL`: Optional model name for the AI assistant.
- `VAPID_PUBLIC_KEY`: Public push notification key.
- `VAPID_PRIVATE_KEY`: Private push notification key.
- `VAPID_CLAIMS_EMAIL`: Contact email for browser push notification claims.

## Normal Update Workflow

1. Edit files locally.
2. Test the site locally if possible.
3. Commit changes to GitHub.
4. Railway redeploys from GitHub.
5. If users installed the web app, they may see an update prompt from the PWA system.

For now, committing to `main` works, but a safer future workflow is:

- Create a feature branch for big changes.
- Test it.
- Merge to `main` only when ready.
- Let Railway deploy from `main`.

## Admin Notes

- Use `/admin` directly if the avatar shortcut is ever unavailable.
- Deleted teams, arenas, and leagues should be archived or removed permanently depending on the admin action.
- Public pages should not show archived/deleted/inactive data.
- If a league has no active teams, the public league page should show an empty state instead of fake standings.

## PWA Notes

- The app install button appears when the browser supports installation.
- Some phones hide install support depending on browser, storage, permissions, or previous install state.
- If the installed app does not reflect a new deployment, open the website in browser and use the update prompt if shown.
- If needed, uninstall and reinstall the web app to force a clean PWA cache.

## Push Notification Notes

Phone notification bar messages require all of these:

- The site must be HTTPS in production.
- The user must grant browser notification permission.
- VAPID keys must be configured in Railway.
- The browser/device must support web push.
- The user must have opened the app after notification support was configured.

## Troubleshooting

- **Railway says Dockerfile missing**: Make sure Railway is set to Nixpacks and `railway.toml` is present.
- **Healthcheck fails**: Confirm `DATABASE_URL` exists and `/health` can start without crashing.
- **`$PORT` error**: Use the Railway start command exactly as written in `railway.toml`.
- **Arenas do not show in create match**: Check that arenas are saved as active records in the database and not archived.
- **Deleted teams or arenas come back**: Check whether the admin action archived, restored, or permanently deleted the item.
- **Phone app does not update**: Use the app update prompt, refresh in browser, or reinstall the PWA.
- **Notifications say not configured**: Add VAPID keys and contact email to Railway variables.

## Development Principle

Goalbazi should feel like a real hosted sports platform, not a local test app. Avoid user-facing words like “local,” “demo,” or “test database” unless they are inside developer documentation like this README.
