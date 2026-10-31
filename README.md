# 🎮 Roblox Radar

### Your Roblox tracking dashboard. Launch, track, and stay in the loop.

Roblox Radar is a desktop application that brings Roblox player tracking, profile exploration, and server finding together in one convenient interface.

Want to keep an eye on when your favorite Roblox players come online? Need to explore a profile or find a game server? Roblox Radar makes it easy to get started.

**No complicated setup. No manual dependency installation. Just launch the app and start tracking!**

---

## ✨ Features

### 👀 Advanced Player Tracking
- Add Roblox players using their username or user ID.
- Check tracked players' online presence and status.
- Keep your tracking list between sessions.
- Configure the refresh interval from 30 to 300 seconds.
- View cached status information when a fresh update is unavailable.

### 🔔 Windows Desktop Notifications
- Receive Windows toast notifications when tracked players change status, when notification support is available.
- Get notified without needing to constantly watch the tracker.
- Automatically check for missing notification packages at startup.
- Get a relaunch option when notification support has been installed during startup.

**Tip:** Keep Roblox Radar running while you want it to monitor tracked players. Windows notifications must be enabled, and the notification package must be available for desktop alerts to work.

### 👤 Full Roblox Profile Viewer
Explore available public Roblox profile information, including:

- Profile details and account creation date
- Avatar images
- Friend and follower counts
- Username history
- Groups
- Favorites
- Official Roblox profile badges

Profile information depends on what Roblox makes available at the time of your request.

### 🌐 Roblox Server Finder
Explore available game servers using a Roblox experience's place ID or game URL.

- Look up supported Roblox experiences.
- Browse available servers.
- Open a selected server through the Roblox app.

Server availability depends on the experience and the information Roblox provides.

### 💾 Local Data Storage
Your tracking information is saved locally so it can be available the next time you launch Roblox Radar.

- Save your tracked player list.
- Cache presence information.
- Preserve tracker preferences.
- Keep useful data between sessions.

### 🎨 Built-In App Interface
Roblox Radar features a dedicated web-based interface with GlobalStats-inspired styling and a built-in loading screen.

Everything is organized in one place, so you can focus on tracking players instead of juggling browser tabs.

---

## 🚀 Get Started

Getting started is simple!

### Step 1: Install Python

Roblox Radar requires Python. Python 3.10 or newer is recommended.

If Python is not already installed, download it from the official Python website:

https://www.python.org/downloads/

On Windows, enable the option to add Python to PATH during installation if it is offered.

### Step 2: Launch Roblox Radar

Place `roblox_radar.py` in your project folder.

Open Command Prompt or PowerShell in that folder and run:

```bash
python roblox_radar.py
```

If the `python` command does not work on Windows, try:

```bash
py roblox_radar.py
```

### Step 3: Let Roblox Radar Handle Setup

That's it!

When Roblox Radar starts, it checks for the packages it needs and attempts to install any missing ones automatically.

You normally do not need to install dependencies manually or manage a separate dependency file.

The automatic installer handles the initial setup for you. An internet connection is required when packages need to be downloaded.

If a required package cannot be installed, check your internet connection and Python installation, then review the error messages in the terminal.

### Step 4: Start Tracking!

Once the application opens:

1. Explore the available tools.
2. Add the Roblox players you want to track.
3. Configure your preferred refresh interval.
4. Enable Windows notifications if you want desktop alerts.
5. Let Roblox Radar help you keep up with player status changes.

**Launch it. Add your players. Stay in the loop.** 🎮

---

## 🔔 Windows Notifications Setup

Roblox Radar supports Windows desktop toast notifications through the `winotify` package.

The app checks for missing packages during startup and attempts to install them automatically.

For notifications to work:

- You must be running Windows.
- The `winotify` package must be installed and available.
- Windows notifications must be enabled in your system settings.
- Roblox Radar must be running to monitor player status.
- If the package was installed during startup, restart the app when prompted so the notification system can initialize correctly.

### Notifications Not Appearing?

Try the following:

1. Check that Windows notifications are enabled.
2. Close and relaunch Roblox Radar if notification support was just installed.
3. Check the terminal for installation errors.
4. Confirm that the package installation completed successfully.

**Important:** If notification support is unavailable, Windows toast notifications will not work until the package is successfully installed and loaded. Other application features may still work normally.

---

## 📁 Local Files

Roblox Radar stores its data locally.

Depending on your version and previous usage, you may see these files:

| File | Purpose |
| --- | --- |
| `roblox_radar.py` | Main application script |
| `tracked_players.json` | Tracked player list and tracker settings |
| `radar_cache.json` | Cached presence information and preferences |
| `~/.roblox_radar.json` | Legacy tracking data that may be imported if present |

The application creates its local data files when needed.

### Moving or Resetting Your Data

If you move Roblox Radar to another folder, keep the relevant data files if you want to preserve your tracking information.

If you want to reset your local data, you can remove the appropriate data files. Back them up first if you want to keep your existing player list or cached information.

---

## 🛠️ Troubleshooting

### Roblox Radar Will Not Launch

- Make sure Python 3.10 or newer is installed.
- Try running `python --version` or `py --version`.
- Launch the script from Command Prompt or PowerShell so you can see error messages.
- Check your internet connection if packages need to be installed.
- Make sure Python can access `pip`.
- Review any installation errors shown in the terminal.

The application attempts to install missing runtime packages automatically, but installation can fail if the internet connection, Python environment, or package service is unavailable.

### A Player's Status Is Not Updating

Roblox Radar relies on Roblox services and publicly available endpoints.

Information may be delayed or unavailable because of:

- Temporary Roblox service issues
- Request rate limits
- Changes to Roblox endpoints
- Network connectivity problems
- Information that Roblox does not currently provide

The application may display cached presence information when a fresh request is unavailable.

A cached status may not reflect the player's current status.

### Windows Notifications Are Not Working

- Confirm that you are running Windows.
- Make sure Windows notifications are enabled.
- Check that `winotify` was installed successfully.
- Relaunch the app if notification support was installed during startup.
- Review the terminal for any installation errors.

### The Server List Is Empty

- Verify that the Roblox place ID or game URL is correct.
- Check your internet connection.
- Try again later if Roblox server information is temporarily unavailable.

### Some Profile Information Is Missing

Not every piece of profile information is always available.

Roblox Radar displays information returned by Roblox services. A missing result does not necessarily mean the information is false or that something is wrong with the account.

The friends list and game badges sections are marked as under construction in the current version.

---

## 🔒 Privacy and Responsible Use

Roblox Radar uses publicly available Roblox endpoints to retrieve supported profile, server, and presence information.

Tracking data and cached information are stored locally on your computer.

Please use the application responsibly and respect other users' privacy.

Roblox Radar is an independent project and is not an official Roblox product.

Roblox may change its services, endpoints, or access limits at any time. These changes can affect the availability or accuracy of certain features.

---

## 🎯 Ready to Get Started?

No unnecessary setup steps. No manual dependency management.

**Just launch Roblox Radar, add the players you care about, and start tracking!**

🎮 **Roblox Radar: Your players. Your dashboard. Your radar.**