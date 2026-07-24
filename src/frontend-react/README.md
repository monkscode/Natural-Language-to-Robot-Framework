# Mark 1 — React Frontend

React + Vite + TypeScript + shadcn/ui conversion of the original HTML/JS/CSS frontend.

## Stack

| Layer | Choice |
|---|---|
| Framework | React 18 + TypeScript |
| Build | Vite 5 |
| Styling | Tailwind CSS v3 + shadcn/ui |
| Routing | React Router DOM v6 |
| Icons | Lucide React |
| State | React hooks (no external state lib) |

---

## Quick start

```bash
# 1. Install dependencies
cd src/frontend-react
npm install

# 2. Install shadcn/ui base + all required components
npx shadcn@latest init          # accept defaults; baseColor: neutral; CSS vars: yes

# 3. Add UI components used by the app
npx shadcn@latest add button input textarea label card badge separator select
npx shadcn@latest add sidebar dropdown-menu avatar breadcrumb tooltip

# 4. (Optional) Add the login-03 auth block as a reference
#    The LoginPage.tsx in this repo already implements the same pattern manually.
#    npx shadcn@latest add login-03

# 5. Start the dev server
npm run dev
# → http://localhost:5173
```

> **Backend proxy:** Vite proxies `/api/*` → `http://localhost:5000` (the FastAPI backend).
> Start the backend separately with `./run.sh` from the repo root before testing API calls.

---

## File structure

```
src/frontend-react/
├── index.html                     Vite HTML entry
├── vite.config.ts                 Vite config + /api proxy
├── tailwind.config.ts             Tailwind theme extending shadcn tokens
├── components.json                shadcn CLI config
├── src/
│   ├── main.tsx                   React DOM entry
│   ├── App.tsx                    Router + layout
│   ├── index.css                  Tailwind directives + shadcn CSS vars + neo theme
│   ├── lib/utils.ts               cn() utility
│   ├── components/
│   │   ├── ui/                    ← shadcn generates files here
│   │   ├── app-sidebar.tsx        Sidebar (logo, nav, user footer)
│   │   ├── app-header.tsx         Topbar (breadcrumb, mode toggle, neo toggle)
│   │   └── theme-provider.tsx     Context for color-mode + visual theme
│   └── pages/
│       ├── GeneratePage.tsx       Main feature: NL → Robot Framework
│       ├── HistoryPage.tsx        Past test runs table
│       ├── MetricsPage.tsx        Analytics dashboard
│       ├── SettingsPage.tsx       AI + RF + server config
│       └── auth/
│           ├── LoginPage.tsx      shadcn login-03 pattern
│           ├── SignupPage.tsx     Registration with password strength
│           └── ForgotPasswordPage.tsx  Email reset + check-email state
```

---

## Theming

Mark 1 ships **two visual themes** toggled at runtime via the `Neo` button in the header.

| Class on `<html>` | Effect |
|---|---|
| *(none)* | Professional — shadcn neutral palette, rounded corners, soft shadows |
| `.dark` | Dark mode (any theme) |
| `.neo` | Neobrutalism — acid green `#ccff00`, 0 radius, hard offset shadows, dot-grid bg |
| `.neo.dark` | Dark neobrutalism |

The `ThemeProvider` at `src/components/theme-provider.tsx` manages both dimensions and persists choices to `localStorage`.

The three color-mode options (Light / Dark / System) are in the topbar. **System** respects `prefers-color-scheme` and updates live when the OS setting changes.

---

## Creating the GitHub PR

```bash
# From repo root
git checkout main
git pull

# Create feature branch
git checkout -b feature/react-shadcn-frontend

# Stage only the new frontend
git add src/frontend-react/

# Commit
git commit -m "feat: convert frontend to React + shadcn/ui

- Vite + TypeScript + Tailwind CSS v3 + shadcn/ui
- Sidebar navigation layout (shadcn sidebar-07 pattern)
- Professional + Neobrutalism dual themes
- Light / Dark / System color-mode toggle
- shadcn login-03 auth pages: Login, Signup, Forgot Password
- All original pages ported: Generate, History, Metrics, Templates, Settings

Closes #<issue-number>"

# Push and open PR against develop
git push -u origin feature/react-shadcn-frontend

# On GitHub: open PR  feature/react-shadcn-frontend → develop
# PR title:  feat: React + shadcn/ui frontend
# Draft: yes (until QA sign-off)
```

---

## What's next (not in this PR)

- [ ] Wire `GeneratePage` to real `/api/generate` WebSocket stream
- [ ] Replace mock user in `app-sidebar.tsx` with real auth context
- [ ] Add React Query for History + Metrics API calls
- [ ] Add `react-router-dom` auth guard (`<RequireAuth>` wrapper)
- [ ] Write Vitest unit tests for `GeneratePage` state machine
