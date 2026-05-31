# CryFi

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A local, responsive **web GUI for the [`aircrack-ng`](https://www.aircrack-ng.org/) suite**.
It runs on a headless Linux host (e.g. a ZimaBoard) and is driven from any browser
on your LAN — turning multi-step CLI workflows (monitor mode → scan → capture →
crack) into a clean, point-and-click dashboard, with a built-in wordlist generator.

> ⚠️ **Authorized use only.** This tool controls Wi-Fi hardware and can transmit
> deauthentication frames. Use it **solely** on networks you own or have explicit
> written permission to audit. You are responsible for complying with all
> applicable laws.

---

## Features

| # | Module | What it does |
|---|--------|--------------|
| 1 | **Interfaces** | Lists adapters with **mode, bus (USB/PCI), driver, IPv4 and connected SSID**. One click toggles monitor mode (`airmon-ng`). |
| 2 | **Scan & Target** | Background `airodump-ng` across **2.4 / 5 GHz**, live auto-updating table with **color-coded signal bars** and **WPA2/WPA3 badges**. Click a row → target panel with a **parsed capture dashboard** (data packets, connected clients, 🎉 on handshake) and a collapsible raw view. |
| 3 | **Live Terminal** | Raw stdout stream of any background job over WebSocket. |
| 4 | **Handshakes** | Registry of verified WPA handshakes; one click sends one to the cracker. |
| 5 | **Files** | Captures auto-flagged for handshakes (Analyze / Clean / batch-delete); wordlists with **line counts + preview**. CSV sidecars auto-deleted. |
| 6 | **Crack** | `aircrack-ng` with a **live dashboard**: speed (k/s), progress bar, the passphrase being tried, 🎉 on KEY FOUND, and clear WPA3/empty-capture messages. Stop button. |
| 7 | **Wordlist Studio** | One unified generator: **target words + mutations** (case / leet / reverse / append / suffixes / combine) plus optional **phone masks** and **charset brute-force**, merged & deduped. Streams to **split files** with flat memory — scales to hundreds of millions of lines. Estimate + Preview + live progress + Stop. |

Plus: **login portal**, single-page collapsible layout with a quick-guide, stacking
toast notifications, and a forced-fresh asset cache (no stale JS after updates).

---

## Backend

Python **FastAPI** app (`app/`), served by `uvicorn`. No database — all state is
files on disk plus small in-memory registries.

```
app/
  main.py                 FastAPI app: auth gate + no-cache middleware, routers, static
  config.py               all settings (env-overridable)
  core/
    auth.py               session tokens + credential check
    security.py           regex sanitizers + path-traversal guards
    process_manager.py    subprocess registry; SIGINT→SIGTERM→SIGKILL teardown, no zombies
    csv_parser.py         airodump-ng CSV → APs + clients
    handshakes.py         captured-handshake registry (JSON sidecar)
    wordgen.py            generation engine (charset / phone / words)
    wordgen_jobs.py       threaded streaming generator: split files, disk guard, stop
  routers/
    auth.py               /login /logout /me
    interfaces.py         list (mode/bus/driver/ip/ssid) + monitor start/stop
    scan.py               background scan + polled results (2.4/5 GHz)
    execution.py          targeted capture + aireplay deauth + parsed capture status
    files.py              captures (list/analyze/clean/batch-delete) + wordlists (+lines/preview)
    crack.py              aircrack-ng with live status parsing
    wordlist_gen.py       estimate / preview / generate (background) + job control
  ws/terminal.py          authenticated WebSocket stdout streaming
static/                   index.html + login.html + Tailwind(CDN) + vanilla JS
```

**Design notes**

- **No `shell=True` anywhere** — every command is built as an argv list, so shell
  injection is structurally impossible. Regex sanitization (BSSID, interface,
  channel, band, filename) is a second layer; path-traversal is blocked by
  resolving every path inside its permitted directory.
- **Clean process teardown** — each job runs in its own process group; Stop and
  WebSocket-disconnect signal **SIGINT → SIGTERM → SIGKILL**. `tini` is PID 1
  (`init: true`) so orphaned aircrack children are reaped instead of zombifying.
- **Constant-memory generation** — wordlists stream to split files; dedupe is
  optional and memory-bounded, so output is limited by **disk, not RAM**.
- **Auth** — a single-user session cookie gates every page, API route and
  WebSocket. Static assets and the login endpoints are public.

---

## How to run

### Docker (recommended)

```bash
cd cryfi
docker compose up --build -d
```

Then open **`http://<host-ip>:8000`** from a browser on the same LAN and sign in.

The container runs with `network_mode: host`, `privileged: true` and `NET_ADMIN`/`NET_RAW`
so it can control the host's Wi-Fi adapter. Captures and wordlists persist to
`./captures` and `./wordlists` on the host.

> **Privilege note:** `privileged + host networking` grants broad host access —
> inherent to manipulating Wi-Fi hardware from a container. Run it only on a
> trusted machine you control.

### Login

Default credentials: **`Admin` / `Admin`** — **change them** via the
`CRYFI_USER` / `CRYFI_PASSWORD` env vars in `docker-compose.yml`, or from the
UI (🔑 in the header → *Change password*; persisted hashed to `./data/auth.json`).

### Local (without Docker)

Requires the aircrack-ng suite + `iw` installed, and root (or passwordless sudo).

```bash
pip install -r requirements.txt
CRYFI_USE_SUDO=1 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Typical workflow

1. **Interfaces** → *Start Monitor* on your adapter (becomes e.g. `wlan0mon`).
2. **Scan** → choose the monitor interface + band → *Start Scan* → click a target row.
3. **Capture** → *Capture Handshake*; optionally *Deauth* a client to force a
   reconnect. Watch the dashboard for data packets / clients / the 🎉 handshake.
4. **Wordgen** *(optional)* → build a wordlist tailored to the target.
5. **Files** → confirm the `.cap`, upload or generate a wordlist.
6. **Crack** → pick the `.cap` + wordlist → *Analyze* → watch the live dashboard.

> **5 GHz tip:** needs a 5 GHz-capable adapter, and deauth is frequently blocked
> by regulatory `no-IR` rules there. Target the **2.4 GHz twin** of the same
> router (same password). Note **WPA3 (SAE)** captures are not crackable — only
> WPA2 handshakes are.

---

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `CRYFI_USER` | `Admin` | Login username |
| `CRYFI_PASSWORD` | `Admin` | Login password — **change this** |
| `CRYFI_SESSION_HOURS` | `12` | Session lifetime |
| `CRYFI_USE_SUDO` | `0` | Prefix privileged commands with `sudo` (set `1` for local non-root) |
| `CRYFI_CAPTURES_DIR` | `/app/captures` | Where `.cap` files are written |
| `CRYFI_WORDLISTS_DIR` | `/app/wordlists` | Where wordlists live |
| `CRYFI_DATA_DIR` | `/app/data` | App state (hashed changed-password file) |
| `CRYFI_TERM_GRACE` | `3.0` | Seconds between escalating stop signals |
| `CRYFI_SCAN_POLL_MS` | `2000` | Scan-table refresh cadence (frontend) |
| `CRYFI_MAX_WORDLIST_BYTES` | `524288000` | Upload size cap (500 MB) |
| `CRYFI_WORDGEN_MAX_TARGET` | `1000000000000` | Max lines per generation job (1T) |
| `CRYFI_WORDGEN_DEFAULT_TARGET` | `5000000` | Default target when unspecified |
| `CRYFI_WORDGEN_DEDUPE_CAP` | `20000000` | In-memory dedupe ceiling (then streams on) |
| `CRYFI_WORDGEN_MIN_FREE_BYTES` | `524288000` | Stop a generation job below this free disk |

All of these are wired in `docker-compose.yml`.

---

## License

Released under the **MIT License** — see [`LICENSE`](LICENSE). You may use,
modify and redistribute it freely.

This software is provided **as-is, without warranty**, for authorized security
auditing only. The authors are not responsible for misuse.
