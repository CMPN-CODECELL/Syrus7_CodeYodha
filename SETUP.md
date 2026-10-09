# Running Countersign on your laptop

Takes about 10 minutes the first time. Everything is free and runs locally. Works on Windows, macOS and Linux.

## 1. Install the tools (once)

| Tool | Needed? | What it runs |
|---|---|---|
| Docker Desktop | yes | the database (Postgres) and cache (Valkey) |
| uv | yes | the Python backend (it downloads Python 3.12 by itself if you don't have it) |
| Node.js 22+ and pnpm 10 | yes | the web app |
| Ollama | optional | a local AI model, only useful if you have an NVIDIA GPU or Apple Silicon |

**Windows** (PowerShell):
```powershell
winget install -e --id Docker.DockerDesktop
winget install -e --id astral-sh.uv
winget install -e --id OpenJS.NodeJS.LTS
npm install -g pnpm@10
```
Close and reopen PowerShell after installing, so the new commands are found.

**macOS** (Homebrew):
```bash
brew install --cask docker
brew install uv node pnpm
```

**Linux:** Docker Engine with the compose plugin, uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`), Node.js 22+ and pnpm 10.

## 2. Unzip and install dependencies (once)

Unzip to a normal folder such as `C:\dev\countersign` or `~/dev/countersign`. Avoid OneDrive or iCloud synced folders: syncing thousands of dependency files slows everything down.

Open a terminal in the `countersign` folder (the same commands work in PowerShell and in bash):
```
cd backend; uv sync; cd ..
cd web; pnpm install; cd ..
```

## 3. Add a free AI key (optional, recommended)

Copy `.env.example` to `.env` (`copy .env.example .env` on Windows, `cp .env.example .env` on macOS/Linux). Then paste in a free key:

- **Groq** (fastest, recommended): create a key at https://console.groq.com/keys and put it after `CS_GROQ_API_KEY=`
- **Gemini** (backup): create a key at https://aistudio.google.com/apikey and put it after `CS_GEMINI_API_KEY=`

Without any key the app still works fully: the built-in rule-based engine answers chat requests. Ollama is only worth installing with a GPU (`ollama pull qwen3:4b-instruct-2507-q4_K_M`, about 2.5 GB). On a CPU-only laptop it makes chat slow.

Never commit or share `.env`; it holds your personal keys. `.gitignore` already excludes it.

## 4. Start it

1. Open **Docker Desktop** and wait until it shows "Engine running".
2. In the `countersign` folder run:
   ```
   uv run --project backend python scripts/dev.py
   ```
   The first run downloads the Postgres and Valkey images (a few hundred MB). It is ready when it prints `Countersign is running`.
3. Open **http://localhost:3000**. Use `localhost`, not `127.0.0.1`: passkeys only work on `localhost`. You land on the Countersign landing page.
4. Click **Explore the Countersign Copilot**, then **Create an account**: your name, an email and a password of at least 8 characters. Everything stays on your laptop; the email is only your sign-in name (no email is ever sent). You get your own paper trading account with a sample portfolio.
5. Onboarding, once per account:
   - **App PIN** (required): choose your own 4 digits. Easy ones such as 1234, 4321 or 1111 are refused.
   - **Passkey** (recommended): register this laptop's passkey (Windows Hello, or Touch ID on a Mac).
   - **Authenticator app** (optional): scan the QR code with any TOTP app and enter the 6-digit code.
6. **Enter the Copilot**. Next time, sign in with your email and password, or with **Sign in with a passkey**.

**To stop:** press `Ctrl+C` in that terminal, or run `uv run --project backend python scripts/stop.py`.
**Next time:** just open Docker Desktop, run step 4.2, open the browser and sign in.

## 5. Things to try

- The suggested prompts on the Copilot page: P&L, losers, average price, NIFTY options, "Buy 10 RELIANCE at 2800", a two-step plan, a standing instruction.
- **Attack Lab**, then **Run all attacks**: 9 attacks, all blocked, 0 unapproved trades.
- **Demo controls** (account menu at the top right, or `Ctrl+.`), then **Reset demo**: puts your own paper account and your drafts, mandates and chat back to the start. Other accounts are not touched.
- **Security Center**: your account details, passkeys, authenticator, the kill switch, the invariant monitor and the audit chain. Forgot your PIN? Use **Forgot or locked PIN? Reset it with your password** there.

## 6. Check that everything works (optional)

```
cd backend; uv run pytest -q; cd ..                      # expect: 82 passed
uv run --project backend python scripts/smoke.py         # end-to-end order over HTTP
```
The browser test needs a one-time `pip install playwright httpx` and `playwright install chromium`. Then run `python scripts/e2e_browser.py` (desktop) or `python scripts/e2e_browser.py --device "Pixel 7"` (phone layout); expect `27/27 passed`. It creates two throwaway accounts (`e2e.*@example.test`), walks the whole journey from the landing page and revokes the test passkey at the end. Run the smoke and browser tests while the app is running.

## Troubleshooting

| You see | Fix |
|---|---|
| `error during connect` / `Cannot connect to the Docker daemon` | Docker Desktop is not running. Start it and wait for "Engine running". |
| `port is already allocated` or `did not open port` | Another program uses 3000, 8000, 8100, 55432 or 56379. Run `scripts/stop.py`, or close that program. |
| `! api exited with code 1` (or another process) | Open `.logs/api.log` (or that process's log); the last lines say why. |
| Chat takes 15+ seconds | No AI key and Ollama is running on the CPU. Add a Groq key, or quit Ollama so the built-in engine answers instantly. |
| Passkey prompt fails | Open the app at `http://localhost:3000`, or use the App PIN tab instead. |
| Countersign button is disabled with "kill switch" | Security Center, enter your PIN, **Disengage with PIN**. |
| "Sign-in is paused for this account" | 5 wrong passwords lock sign-in for 30 s (longer after more failures). Wait for the countdown, or use **Sign in with a passkey**. |
| "Request blocked for safety" | Open the app at `http://localhost:3000`. Another address (a phone, a tunnel) must be added to `CS_EXTRA_ORIGINS` in `.env`. |
| Forgot your PIN, or it is locked | Security Center, **Forgot or locked PIN? Reset it with your password** (plus your authenticator code if you linked one). |
| Forgot your password | There is no email reset (nothing leaves your laptop). Create a new account; it gets its own fresh paper portfolio. |
| You only want to see the UI | `cd web; pnpm dev:mock` runs the web app alone on sample data. Sign up there too (accounts stay in that browser); the authenticator code is `123456`. |

## Phones

The interface adapts to phone screens: it has a bottom tab bar and has been checked at iPhone and Pixel sizes. But the app runs and is opened on the same laptop: the web server, the API and the paper broker all listen on 127.0.0.1 only, so nothing is reachable from the Wi-Fi. Serving phones would need extra configuration, and passkeys on a phone need HTTPS. See `docs/LIMITATIONS.md`.
