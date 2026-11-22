<div align="center">

<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 880 180" width="100%" max-width="880" style="background:#0c0d14; border-radius:18px; border:1px solid rgba(161,138,255,0.24); box-shadow:0 20px 50px rgba(0,0,0,0.65);">
  <defs>
    <!-- Background Gradients -->
    <radialGradient id="bgGlow" cx="20%" cy="50%" r="60%">
      <stop offset="0%" stop-color="#7c3aed" stop-opacity="0.22" />
      <stop offset="100%" stop-color="#0c0d14" stop-opacity="0" />
    </radialGradient>
    <radialGradient id="radarSweepGrad" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#00e676" stop-opacity="0.35" />
      <stop offset="100%" stop-color="#00e676" stop-opacity="0" />
    </radialGradient>
    <linearGradient id="textGrad" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#ffffff" />
      <stop offset="50%" stop-color="#d8cdff" />
      <stop offset="100%" stop-color="#00e676" />
    </linearGradient>
    <linearGradient id="subTextGrad" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#a18aff" />
      <stop offset="100%" stop-color="#00d2ff" />
    </linearGradient>
    <filter id="neonGlow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="3.5" result="blur" />
      <feMerge>
        <feMergeNode in="blur" />
        <feMergeNode in="SourceGraphic" />
      </feMerge>
    </filter>
  </defs>

  <!-- Ambient Glow -->
  <rect width="880" height="180" fill="url(#bgGlow)" rx="18" />

  <!-- Tactical Radar Scope Icon -->
  <g transform="translate(40, 25)">
    <!-- Outer Scope Ring -->
    <circle cx="65" cy="65" r="62" fill="#090a10" stroke="rgba(0, 230, 118, 0.3)" stroke-width="2.5" />
    <circle cx="65" cy="65" r="46" fill="none" stroke="rgba(0, 230, 118, 0.18)" stroke-width="1.2" stroke-dasharray="3,3" />
    <circle cx="65" cy="65" r="28" fill="none" stroke="rgba(0, 230, 118, 0.22)" stroke-width="1.2" />
    <circle cx="65" cy="65" r="12" fill="none" stroke="rgba(0, 230, 118, 0.3)" stroke-width="1.2" />

    <!-- Crosshair Lines -->
    <line x1="3" y1="65" x2="127" y2="65" stroke="rgba(0, 230, 118, 0.25)" stroke-width="1.2" />
    <line x1="65" y1="3" x2="65" y2="127" stroke="rgba(0, 230, 118, 0.25)" stroke-width="1.2" />

    <!-- Radar Sweep Sector -->
    <path d="M65 65 L108 22 A62 62 0 0 1 127 65 Z" fill="url(#radarSweepGrad)" />
    <line x1="65" y1="65" x2="127" y2="65" stroke="#00e676" stroke-width="2" filter="url(#neonGlow)" />

    <!-- Blip Targets -->
    <circle cx="98" cy="44" r="3.5" fill="#00e676" filter="url(#neonGlow)" />
    <circle cx="98" cy="44" r="6.5" fill="none" stroke="#00e676" stroke-width="1" opacity="0.6" />
    <circle cx="48" cy="85" r="3" fill="#00d2ff" filter="url(#neonGlow)" />
    <circle cx="82" cy="98" r="2.8" fill="#ffb300" />

    <!-- Center Icon Mark -->
    <circle cx="65" cy="65" r="3.5" fill="#00e676" />
  </g>

  <!-- Typography: Primary Brand -->
  <g transform="translate(195, 82)">
    <text font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif" font-weight="900" font-size="44" letter-spacing="4" fill="url(#textGrad)">
      ROBLOX RADAR
    </text>
  </g>

  <!-- Typography: Subtitle / Author -->
  <g transform="translate(198, 118)">
    <text font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif" font-weight="700" font-size="14.5" letter-spacing="6" fill="url(#subTextGrad)">
      BY GLOBALSTATS
    </text>
  </g>

  <!-- Telemetry HUD Accents -->
  <g transform="translate(198, 142)">
    <text font-family="'Consolas', 'Courier New', monospace" font-size="11" letter-spacing="2" fill="rgba(255,255,255,0.4)">
      TACTICAL TELEMETRY SUITE · REAL-TIME PUBLIC INTELLIGENCE
    </text>
  </g>

  <!-- Right Decorative Telemetry Status -->
  <g transform="translate(735, 48)">
    <rect width="105" height="26" rx="6" fill="rgba(0,230,118,0.08)" stroke="rgba(0,230,118,0.3)" stroke-width="1" />
    <circle cx="14" cy="13" r="4" fill="#00e676" filter="url(#neonGlow)" />
    <text x="26" y="17" font-family="'Consolas', monospace" font-size="10" font-weight="700" letter-spacing="1" fill="#00e676">SYS LIVE</text>

    <text x="100" y="55" text-anchor="end" font-family="'Consolas', monospace" font-size="10" letter-spacing="1.5" fill="rgba(255,255,255,0.35)">RANGE: OMNI</text>
    <text x="100" y="75" text-anchor="end" font-family="'Consolas', monospace" font-size="10" letter-spacing="1.5" fill="rgba(255,255,255,0.35)">FREQ: 9.4 GHz</text>
  </g>
</svg>

<p align="center" style="margin-top: 16px;">
  <img src="https://img.shields.io/badge/Python-3.9+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.9+" />
  <img src="https://img.shields.io/badge/GUI-pywebview-1E1E2E?style=for-the-badge" alt="pywebview" />
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-4A154B?style=for-the-badge" alt="Cross Platform" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License" />
</p>

<p align="center">
  A high-performance desktop telemetry dashboard and public intelligence interface for the Roblox platform. Built with Python, pywebview, and direct public web services.
</p>

</div>

---

## Architecture & Core Modules

Roblox Radar operates via zero-credential public endpoints without requiring session tokens, web scraping, or authenticated user accounts.

```
+-------------------------------------------------------------------------------+
|                                  ROBLOX RADAR                                 |
+-------------------------------------------------------------------------------+
|  [Tactical Radar Scope]    [Live Presence Engine]    [Server Discovery Hub]   |
|  - 360 deg Polar Sweep     - Delta State Detection   - Ping & Latency Gauges  |
|  - Target Distance Rings   - Native Toast Alerts     - Direct Protocol Launch |
+-------------------------------------------------------------------------------+
|  [Profile Intelligence]   [Group Explorer]           [Catalog Engine]         |
|  - Dossier & Signals       - Structure & Roles       - Real-time Price Lookup |
|  - Headshot Thumbnails     - Community Experience    - High-Res Visual Assets |
+-------------------------------------------------------------------------------+
```

---

## Functional Capabilities

### Player Presence Radar
* **360-Degree Tactical Scope:** Real-time canvas-based sonar sweep visualizing tracked player presence by status tier (In Game, In Studio, Website/App, Offline).
* **Target Lock-On:** Interactive blips on the radar display that scroll to and highlight player dossiers.
* **State Transition Notifications:** Native desktop toast alerts featuring cached user avatar headshots and status change descriptions.
* **Audio Telemetry System:** Procedural Web Audio synthesizer generating sonar sweep tones, lock-on pings, and tactile click feedback.

### Server Discovery Engine
* **Performance Telemetry:** Displays player capacity density, server frames per second (FPS), and real-time network latency metrics with dynamic color thresholds.
* **One-Click Launch:** Protocol handler invocation (`roblox://experiences/start?...`) for immediate client connection to active server instances.
* **Filter Controls:** Capacity thresholds and full-server exclusion filters.

### Profile Intelligence & Comparison
* **Public Profile Dossier:** Account creation chronology, badge records, official verification flags, and ban states.
* **Side-by-Side Comparison:** Comparative audit between multiple accounts analyzing friend, follower, and following counts.
* **High-Resolution Asset Resolution:** Native integration with official Roblox thumbnail delivery infrastructure.

### Group & Catalog Discovery
* **Group Structure Explorer:** Supports both v1 and v2 group hierarchies, returning ownership, verified insignia, member counts, community experiences, and rank divisions.
* **Catalog Matrix:** Queries public catalog listings, showing accurate Robux prices, free item tags, creator attribution, and asset imagery.

---

## Operating System Setup & Execution

### Prerequisites

| Component | Minimum Version | Notes |
| :--- | :--- | :--- |
| **Python** | 3.9 or higher | Ensure `pip` is installed and added to your system path. |
| **Web Runtime** | WebView2 / WebKit | Windows 10/11 includes WebView2 by default. |

---

### Installation

Clone the repository or download the project archive into your workspace:

```bash
git clone https://github.com/your-username/roblox-radar.git
cd "roblox-radar"
```

Install the required system packages:

```bash
python -m pip install --upgrade pip
python -m pip install pywebview requests
```

> **Note for Windows Users:** To enable native Windows toast notifications with custom user avatars, install `winotify`:
> ```bash
> pip install winotify
> ```

---

### Platform-Specific Instructions

#### Windows

You can launch using the included batch runner or via terminal:

```cmd
setup_roblox_radar.bat
```

Or manually:

```cmd
python roblox_radar.py
```

#### macOS

Install `pywebview` along with PyObjC dependencies:

```bash
pip install pywebview requests pyobjc-framework-WebKit
python3 roblox_radar.py
```

#### Linux (Ubuntu / Debian)

Install the required GTK/WebKit development libraries before launching:

```bash
sudo apt-get update
sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
pip install pywebview requests
python3 roblox_radar.py
```

---

## Data Storage & Local Persistence

Roblox Radar stores configuration and history locally on the host machine. No external telemetry servers are contacted.

| Path | Format | Purpose |
| :--- | :--- | :--- |
| `tracked_players.json` | JSON | Registry of added user IDs, display names, and pause statuses. |
| `radar_cache.json` | JSON | Local response cache and interval preferences. |
| `radar_history.sqlite3` | SQLite | Time-series data points for watched games and presence changes. |
| `~/.cache/roblox_radar/toast_icons/` | PNG | Cached player avatars for operating system toast notifications. |

---

## Technical Specifications & Privacy

* **Direct Web API Architecture:** Interfaces directly with Roblox public web endpoints without third-party proxies.
* **Rate-Limit Compliance:** Built-in cooldown throttles prevent request flooding and respect service rate limits.
* **Zero Credential Requirement:** Operates without Roblox `.ROBLOSECURITY` cookies, passwords, or authentication tokens.
* **No Telemetry Collection:** All operational metrics, tracked targets, and cache files remain entirely local.

---

## License

Distributed under the terms of the MIT License. See `LICENSE` for further details.
