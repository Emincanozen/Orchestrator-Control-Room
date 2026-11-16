# Installation (Windows 10/11)

The orchestrator runs entirely on your machine. There is nothing to install system-wide: the program lives in this
folder, keeps its data under `%LOCALAPPDATA%` (see section 3), and listens on `127.0.0.1` only.

## 1. Prerequisites

| Tool | Version | Why |
|---|---|---|
| Windows | 10 or 11 | target platform |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | recent | creates the Python 3.12 environment and runs the backend |
| [Node.js](https://nodejs.org) | 22 LTS (includes npm) | builds the dashboard (once) |
| [Git for Windows](https://git-scm.com) | any recent | every task works in a git worktree |

The three provider CLIs are **optional**, and you install them and sign in yourself:
Claude Code (`claude`), OpenAI Codex (`codex`) and Google Antigravity (`agy`). The orchestrator only detects
them; it never installs them and never stores your credentials. Without any of them the dashboard and CLI
still work, but no task can run on a real model.

## 2. First start

Double-click `scripts\start.cmd` (or run it from a terminal). On the first run it:

1. checks that `uv` and `node`/`npm` are installed and tells you what is missing;
2. runs `uv sync` if `backend\.venv` does not exist;
3. runs `npm.cmd ci` and `npm.cmd run build` if `frontend\dist` does not exist;
4. runs `orchestrator dashboard`, which starts the server in the background and opens your browser.

The first start takes a few minutes (downloads); later starts take a few seconds. Stop the background server with
`scripts\stop.cmd` or `orchestrator stop`.

Check the setup at any time (local checks only, no model call, no quota spent):

```
scripts\orchestrator.cmd doctor
```

### The command line

`scripts\orchestrator.cmd` runs the CLI from any directory, in cmd.exe and in PowerShell. It is a `.cmd` file, so
the PowerShell ExecutionPolicy does not apply:

```
scripts\orchestrator.cmd status
scripts\orchestrator.cmd projects add shop D:\code\shop --verify-cmd "npm test"
scripts\orchestrator.cmd run --project prj_... --title "Fix the login bug" --follow
```

Every command has `--help` with examples and `--json` for machine-readable output. Exit codes: `0` ok, `1`
API or usage error, `2` backend not reachable. The CLI keeps no state: it talks to the running backend with the
token from `api-token` in the data folder, exactly like the dashboard.

#### Putting `scripts\` on your PATH (optional, manual)

There is deliberately no installer script. To type `orchestrator` anywhere, add the folder to your *user* PATH
yourself (no admin rights needed):

1. Press the Windows key, type `environment variables`, open **Edit environment variables for your account**.
2. Select `Path` under *User variables*, click **Edit**, then **New**, and paste the full path of the `scripts`
   folder (for example `C:\path\to\AI-Orchestrator\scripts`).
3. Click OK on all dialogs and open a **new** terminal.
4. In cmd.exe or PowerShell, `orchestrator.cmd status` works from anywhere. In cmd.exe `orchestrator status` works
   too; in PowerShell type `orchestrator.cmd` (PowerShell would look for `orchestrator.ps1` first).

To undo, remove the same entry.

## 3. Where your data lives

Your data is **not** in the repository. The default data folder is `%LOCALAPPDATA%\AI-Orchestrator`
(non-Windows: `$XDG_DATA_HOME` or `~/.local/share`, plus `/ai-orchestrator`):

| Path | Content |
|---|---|
| `orchestrator.db` | the SQLite database (projects, tasks, events, settings) |
| `api-token` | the API token (random, created on first start) |
| `logs\server.log` | log of the background server |
| `server.pid` | pid of the background server (used by `orchestrator stop`) |
| `server.lock` | single-instance lock; a second server on the same data folder fails fast |
| `skills\` | installed skills |

Task worktrees (one git worktree per task) live in a **separate** sibling folder,
`%LOCALAPPDATA%\AI-Orchestrator-worktrees`; leader and advisor scratch space is `_scratch\leader` inside it. The two
folders must not contain each other (startup refuses that layout). The repository's old `data\` folder is no longer
used (it is still git-ignored); there is no automatic migration, and `orchestrator doctor` warns if a legacy
`data\orchestrator.db` is still there.

The data folder, the worktrees folder and the token file get an owner-only permission list (current user, SYSTEM and
Administrators; inheritance removed) set through `icacls`. This keeps other accounts on the PC out; it does not protect
against programs running as you. `orchestrator doctor` checks it. Setting `ORCHESTRATOR_HARDEN_ACL=0` disables the
hardening (used by tests; not recommended).

To keep the data somewhere else, set `ORCHESTRATOR_DATA_DIR` (and optionally `ORCHESTRATOR_WORKTREES_DIR`)
**before** starting, and keep them set for the CLI, which reads the token from the same folder. Without
`ORCHESTRATOR_WORKTREES_DIR` the worktrees folder is `<data folder>-worktrees`:

```
setx ORCHESTRATOR_DATA_DIR "E:\orchestrator-data"
```

`setx` only affects terminals opened afterwards. To move existing data: stop the server, move the folder, set the
variable, start again. The CLI also honours `ORCHESTRATOR_TOKEN_FILE` (path of the token file) and
`ORCHESTRATOR_URL` (backend URL) when the backend runs elsewhere than the default.

## 4. Ports

The default is `127.0.0.1:8765` (`security.port` in `config\policies.yaml`). Use another port with
`orchestrator dashboard --port 9000` or `orchestrator serve --port 9000`; point the CLI at it with
`--url http://127.0.0.1:9000` or `ORCHESTRATOR_URL`. The dashboard dev server (`npm.cmd run dev`) uses 5173; for it, start the backend with `ORCHESTRATOR_DEV=1` (that variable also enables `/docs`, `/redoc`, `/openapi.json` and the 5173 origin, which do not exist otherwise).

## 5. Updating

```
scripts\stop.cmd
git pull
uv sync --project backend
cd frontend
npm.cmd ci
npm.cmd run build
cd ..
scripts\start.cmd
```

Database migrations run automatically on start. Your data folder (outside the repository) is not touched by `git pull`.

## 6. Uninstalling

1. `scripts\stop.cmd`
2. Remove the `scripts` PATH entry if you added one.
3. Delete the repository folder. That removes the environment (`backend\.venv`) and the build, but **not** your data:
   also delete `%LOCALAPPDATA%\AI-Orchestrator` (database, token, logs, skills) and
   `%LOCALAPPDATA%\AI-Orchestrator-worktrees` (task worktrees), or the folders named by `ORCHESTRATOR_DATA_DIR` /
   `ORCHESTRATOR_WORKTREES_DIR` if you set them. Stop the server first and copy the data folder if you want to keep it.

Nothing else was installed or changed on the system. `uv` and Node keep their own caches, which you can clear with
`uv cache clean` and `npm.cmd cache clean --force`.

## 7. Troubleshooting

**`npx` / `npm` fails with "running scripts is disabled on this system" (PowerShell).**
ExecutionPolicy blocks `.ps1` shims. Use the `.cmd` entries instead: `npm.cmd`, `npx.cmd`, and the repo's
`scripts\*.cmd`. No policy change is needed or recommended.

**"port 8765 is in use" / the dashboard does not start.**
Another program (or another orchestrator started with `serve` in a terminal) owns the port. `orchestrator doctor`
tells you whether it is an orchestrator. Start on another port: `orchestrator dashboard --port 8766`, or stop the
other program. `orchestrator stop` only stops a server it started itself (checked by pid and command line).

**`cannot reach the backend` (exit code 2).**
The backend is not running (or runs on another port). Start it with `orchestrator dashboard` or `orchestrator serve`,
or pass `--url` / set `ORCHESTRATOR_URL`.

**401 "missing or invalid bearer token", or the dashboard asks for a token.**
The CLI/browser token does not match the running server. Typical causes: `ORCHESTRATOR_DATA_DIR` differs between
the terminal that started the server and the one running the CLI, or `api-token` was recreated while the server
was running (restart it). `orchestrator dashboard` opens the browser with the right token in the address (as a `#token=`
fragment, which is never sent to the server); the file `api-token` in the data folder holds the same value if you
need to paste it manually. Never share it.

**A provider shows "not installed" or "not logged in".**
Run `orchestrator providers refresh` after installing the CLI or signing in. `orchestrator doctor` lists what is
detected. Make sure `claude --version`, `codex --version` and `agy --version` work in a normal terminal first.
Use your own installed CLI's subscription login; credentials are never part of the source release.
Current production Codex execution is blocked pending native isolation validation. Its owned-runtime
implementation is designed to read one bounded subscription authentication snapshot into a protected
invocation-local runtime and remove the temporary material during cleanup. It does not copy your settings
or support API-key authentication. See [SECURITY.md](SECURITY.md) before enabling real work.

**"another AI Orchestrator instance is already using this data directory".**
A server is already running on that data folder (`server.lock`). Use that one (`orchestrator dashboard`), or stop it with
`orchestrator stop`.

**Browser console shows a WebSocket close with code 4403.**
The server rejected the `Origin`/`Host`. Open the dashboard at `http://127.0.0.1:<port>/` or
`http://localhost:<port>/` (the port the server actually runs on; ports other than the default work when started
with `--port`). Other hostnames, reverse proxies and network addresses are rejected on purpose. A 4401 close means a wrong token.

**The dashboard page says the frontend is not built.**
`frontend\dist` is missing. Run `scripts\start.cmd`, or `cd frontend`, `npm.cmd ci`, `npm.cmd run build`.

**Something else.**
Read `logs\server.log` in the data folder (background server) or the terminal output (`orchestrator serve`).

## 8. Security notes

- Windows command verification requires an Orchestrator-owned Git worktree and a working AppContainer setup.
  Setup creates a new task-specific AppContainer profile; it does not change the original Python or Node
  installation's permissions. Failure to establish isolation blocks the command.
- Python and Node/npm verification use validated private runtime copies under the Orchestrator installation's
  `data/verification-runtimes/` directory (separate from the configurable database/state directory).
  Node currently requires the standard Program Files installation. `npm test` uses an empty user/global npm
  configuration and an isolated cache; `npx` is offline and only uses dependencies already present in the
  task worktree. Dependency installation, profile credentials, custom Node installations and native build
  toolchains are not validated by this verification path. Provision project dependencies separately before
  verification; do not grant the sandbox access to your existing profile or installation to make a test pass.
- The server binds `127.0.0.1` only. `orchestrator serve --host 0.0.0.0` is refused unless you also pass
  `--i-understand-exposing-the-dashboard`; do not do that on a shared network: the API can start agents that
  edit files on your machine.
- Every API route except `/api/health` (and the WebSocket) needs the bearer token from `api-token` in the data folder.
  The CLI reads the token from that file; it is never accepted as a command-line argument and never printed.
  `orchestrator dashboard` (and `serve --browser`) opens the browser at `/#token=<token>`: the token is in the URL
  fragment, which the browser never sends to the server, and the page removes it from the address bar. The dashboard
  and `orchestrator follow` send the token to the WebSocket as a subprotocol, not in the URL (the server still accepts
  `?token=` for compatibility).
- Treat `api-token` like a password: anyone who can read it can control the orchestrator. The data folder, the
  worktrees folder and the token file are restricted to your account (plus SYSTEM and Administrators) with an ACL.
- Agents and verification commands started by the orchestrator cannot use its API: on Windows they run in a job object
  and the server refuses their requests with 403 `worker_origin_refused`. Verification additionally requires a
  task-specific AppContainer with no network capabilities or loopback exemption. Provider confinement differs;
  see [SECURITY.md](SECURITY.md). N2 remains open pending the remaining security and product acceptance checks.
- `Host` and `Origin` headers are validated (DNS-rebinding and cross-site protection). Requests without an `Origin`
  (the CLI, curl) are accepted but still need the token.
- Provider credentials stay in the provider CLIs' own stores; the orchestrator never copies them and passes child
  processes an allowlisted environment only (never an `ORCHESTRATOR_*` variable).
