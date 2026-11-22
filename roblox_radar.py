"""
Roblox Radar - pywebview Edition (GlobalStats Style)
Includes: Server Finder, Full Profile Viewer, Enhanced Player Tracker with Local JSON Caching,
and Built-in GlobalStats Loading Splash Screen.

Usage: python roblox_radar.py
"""

import json
import os
import re
import sys
import tempfile
import time
import importlib.util
import subprocess
import threading
import site
from datetime import datetime, timezone
import webbrowser
from pathlib import Path

# Install missing runtime dependencies before importing the application modules.
# requests and pywebview are required; winotify is optional and only used on Windows.
def _add_user_site_to_path():
    """Make packages installed with pip --user visible to this interpreter."""
    try:
        user_site = site.getusersitepackages()
        if isinstance(user_site, str) and user_site and user_site not in sys.path:
            site.addsitedir(user_site)
        importlib.invalidate_caches()
    except (AttributeError, OSError, TypeError, ValueError):
        # The standard site directory is normally already configured; keep
        # startup resilient on embedded or customized Python installations.
        pass


_add_user_site_to_path()


def _ensure_package(import_name, package_name, required=True):
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False

def _bootstrap_dependencies():
    """Install missing packages and return packages installed in this launch."""
    packages = [("requests", "requests", True), ("webview", "pywebview", True)]
    if os.name == "nt":
        packages.append(("winotify", "winotify", False))

    missing = [(module, package, required)
               for module, package, required in packages
               if not _ensure_package(module, package, required)]
    if not missing:
        return []

    print("[Roblox Radar] Packages not found: " +
          ", ".join(package for _, package, _ in missing), flush=True)
    print("[Roblox Radar] Running installer...", flush=True)
    failed_required = []
    installed_packages = []
    for module, package, required in missing:
        try:
            subprocess.check_call([
                sys.executable, "-m", "pip", "install", "--user", package
            ])
            # A successful pip exit means installation occurred even when the
            # current interpreter has a stale import cache or user-site path.
            # Record it so the UI can offer a restart for optional packages.
            installed_packages.append(package)
            _add_user_site_to_path()
            if _ensure_package(module, package, required):
                print(f"[Roblox Radar] Installed {package}; import is now available.", flush=True)
            else:
                print(
                    f"[Roblox Radar] Installed {package}, but it is not visible in this process yet; "
                    "a relaunch is required to initialize it.",
                    flush=True
                )
                if required:
                    failed_required.append(package)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"[Roblox Radar] Could not install {package}: {exc}", flush=True)
            if required:
                failed_required.append(package)

    if failed_required:
        print("[Roblox Radar] Required packages could not be installed: " +
              ", ".join(failed_required), flush=True)
        print("[Roblox Radar] Check your internet connection and Python/pip setup, then try again.",
              flush=True)
        raise SystemExit(1)

    print("[Roblox Radar] Dependencies checked; launching app...", flush=True)
    return installed_packages


STARTUP_INSTALLED_PACKAGES = _bootstrap_dependencies()
WINDOWS_NOTIFICATIONS_NEED_RESTART = (
    os.name == "nt" and "winotify" in STARTUP_INSTALLED_PACKAGES
)
try:
    import requests
    import webview
except ImportError as exc:
    print(f"[Roblox Radar] A required package is still unavailable: {exc}", flush=True)
    raise SystemExit(1) from exc

# --------------------------------------------------------------------------- #
#  Storage paths in the current directory (alongside the script)
# --------------------------------------------------------------------------- #
SCRIPT_DIR = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
TRACKED_FILE = SCRIPT_DIR / "tracked_players.json"
CACHE_FILE = SCRIPT_DIR / "radar_cache.json"
LEGACY_FILE = Path.home() / ".roblox_radar.json"

# --------------------------------------------------------------------------- #
#  Python <-> JavaScript API layer
# --------------------------------------------------------------------------- #
class RobloxAPI:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Origin": "https://www.roblox.com",
            "Referer": "https://www.roblox.com/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty"
        })
        self._csrf = None
        self.tracked = self._load()

    def get_clipboard_text(self):
        """Read clipboard text through the host OS when WebView clipboard APIs are unavailable."""
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            root.update()
            value = root.clipboard_get()
            root.destroy()
            return {"text": str(value or "")}
        except Exception:
            try:
                import pyperclip
                return {"text": str(pyperclip.paste() or "")}
            except Exception as exc:
                return {"text": "", "error": f"Clipboard access failed: {exc}"}

    def _recover_json(self, path, default):
        """Read JSON safely; quarantine malformed data so the app can recover."""
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            try:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                path.replace(path.with_name(f"{path.name}.corrupt-{stamp}"))
            except OSError:
                pass
            return default

    def _atomic_write_json(self, path, data):
        """Write in the same directory then atomically replace the destination."""
        temp_path = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent),
                                             prefix=f".{path.name}.", suffix=".tmp", delete=False) as f:
                temp_path = Path(f.name)
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
            return True
        except (OSError, TypeError, ValueError):
            if temp_path:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    def _load(self):
        data = self._recover_json(TRACKED_FILE, None)
        if isinstance(data, list):
            # Normalize older player records while preserving persisted pause state.
            return [u for u in data if isinstance(u, dict) and u.get("id") is not None]

        legacy = self._recover_json(LEGACY_FILE, [])
        if isinstance(legacy, list):
            self._atomic_write_json(TRACKED_FILE, legacy)
            return [u for u in legacy if isinstance(u, dict) and u.get("id") is not None]
        return []

    def _save_tracked(self, data=None):
        return self._atomic_write_json(TRACKED_FILE, self.tracked if data is None else data)

    def _load_cache(self):
        data = self._recover_json(CACHE_FILE, {})
        return data if isinstance(data, dict) else {}

    def _save_cache(self, cache_data):
        return self._atomic_write_json(CACHE_FILE, cache_data)

    def get_tracker_settings(self):
        cache = self._load_cache()
        settings = cache.get("settings", {})
        if not isinstance(settings, dict):
            settings = {}
        interval = settings.get("refreshIntervalSeconds", 60) if isinstance(settings, dict) else 60
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            interval = 60
        supported = os.name == "nt"
        return {
            "refreshIntervalSeconds": max(30, min(300, interval)),
            "windowsNotificationsEnabled": bool(settings.get("windowsNotificationsEnabled", False)),
            "notificationSoundEnabled": bool(settings.get("notificationSoundEnabled", False)),
            "windowsNotificationsSupported": supported,
            "windowsNotificationsNeedRestart": bool(WINDOWS_NOTIFICATIONS_NEED_RESTART)
        }

    def set_tracker_interval(self, seconds):
        try:
            seconds = max(30, min(300, int(seconds)))
        except (TypeError, ValueError):
            return {"error": "Choose a refresh interval from 30 to 300 seconds."}
        cache = self._load_cache()
        settings = cache.get("settings", {})
        if not isinstance(settings, dict):
            settings = {}
        settings["refreshIntervalSeconds"] = seconds
        cache["settings"] = settings
        self._save_cache(cache)
        return {"refreshIntervalSeconds": seconds}

    def set_windows_notifications_enabled(self, enabled):
        if enabled and os.name != "nt":
            return {"error": "Windows toast notifications are only available on Windows."}
        cache = self._load_cache()
        settings = cache.get("settings", {})
        if not isinstance(settings, dict):
            settings = {}
        settings["windowsNotificationsEnabled"] = bool(enabled)
        cache["settings"] = settings
        self._save_cache(cache)
        return {"windowsNotificationsEnabled": bool(enabled)}

    def set_notification_sound_enabled(self, enabled):
        cache = self._load_cache()
        settings = cache.get("settings", {})
        if not isinstance(settings, dict):
            settings = {}
        settings["notificationSoundEnabled"] = bool(enabled)
        cache["settings"] = settings
        self._save_cache(cache)
        return {"notificationSoundEnabled": bool(enabled)}

    def send_windows_notification(self, title, message, sound_enabled=None, icon_url=None):
        if os.name != "nt":
            return {"error": "Windows toast notifications are only available on Windows."}
        try:
            from winotify import Notification, audio
            if sound_enabled is None:
                sound_enabled = self.get_tracker_settings()["notificationSoundEnabled"]
            local_icon_path = ""
            if icon_url and str(icon_url).startswith("http"):
                try:
                    import hashlib
                    cache_dir = Path.home() / ".cache" / "roblox_radar" / "toast_icons"
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    fn = hashlib.md5(icon_url.encode()).hexdigest() + ".png"
                    img_path = cache_dir / fn
                    if not img_path.exists():
                        r = requests.get(icon_url, timeout=4)
                        if r.status_code == 200:
                            img_path.write_bytes(r.content)
                    if img_path.exists():
                        local_icon_path = str(img_path.resolve())
                except Exception:
                    local_icon_path = ""
            toast = Notification(
                app_id="Roblox Radar",
                title=str(title)[:80],
                msg=str(message)[:220],
                icon=local_icon_path if local_icon_path else ""
            )
            if sound_enabled:
                toast.set_audio(audio.Default, False)
            toast.show()
            return {"ok": True}
        except ImportError:
            return {"error": "Windows toast package is unavailable. Close and relaunch Roblox Radar to retry setup, or run: python -m pip install --user winotify"}
        except Exception as exc:
            return {"error": f"Windows notification could not be shown: {exc}"}

    def relaunch_app(self):
        """Start a fresh app process so newly installed notification support is loaded."""
        try:
            if getattr(sys, "frozen", False):
                # Support packaged builds as well as normal Python script launches.
                command = [sys.executable, *sys.argv[1:]]
            else:
                script_path = Path(sys.argv[0]).resolve()
                if not script_path.is_file():
                    return {"error": "Could not locate the app script. Close and reopen Roblox Radar manually."}
                command = [sys.executable, str(script_path), *sys.argv[1:]]

            subprocess.Popen(command, cwd=str(SCRIPT_DIR), close_fds=True)

            # Let the JavaScript API call return before closing the current window.
            current_window = getattr(self, "_window", None)
            if current_window is not None:
                close_timer = threading.Timer(0.75, current_window.destroy)
                close_timer.daemon = True
                close_timer.start()
            return {"ok": True}
        except Exception as exc:
            return {
                "error": f"Could not relaunch automatically: {exc}. Close and reopen Roblox Radar manually."
            }

    def _request(self, method, url, **kw):
        for _ in range(2):
            headers = kw.pop("headers", {}) or {}
            if self._csrf:
                headers["X-CSRF-TOKEN"] = self._csrf
            try:
                r = self.session.request(method, url, headers=headers, timeout=15, **kw)
            except Exception as e:
                raise RuntimeError(f"Network error: {e}")
            
            # Handle CSRF challenge
            if r.status_code == 403 and "x-csrf-token" in {k.lower() for k in r.headers}:
                self._csrf = next(v for k, v in r.headers.items() if k.lower() == "x-csrf-token")
                continue
            return r
        return r

    def _request_json(self, method, url, **kw):
        """Return a decoded successful response and keep unavailable fields distinct from zero."""
        try:
            response = self._request(method, url, **kw)
            self._last_request_auth_required = response.status_code == 401
            if response.status_code != 200:
                return {}, False
            payload = response.json()
            return (payload if isinstance(payload, dict) else {}), True
        except Exception:
            return {}, False

    # ---------- Core User Logic ----------
    def _lookup_user(self, query: str):
        query = str(query).strip()
        if query.isdigit():
            r = self._request("GET", f"https://users.roblox.com/v1/users/{query}")
            if r.status_code == 200:
                data = r.json()
                return {
                    "id": data["id"],
                    "name": data["name"],
                    "displayName": data.get("displayName", data["name"])
                }
            return {"error": f"User ID {query} not found (HTTP {r.status_code})"}
        
        r = self._request("POST", "https://users.roblox.com/v1/usernames/users",
                          json={"usernames": [query], "excludeBannedUsers": False})
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                return {
                    "id": data[0]["id"],
                    "name": data[0]["name"],
                    "displayName": data[0].get("displayName", data[0]["name"])
                }
            return {"error": f"Username '{query}' not found"}
        return {"error": f"Lookup failed (HTTP {r.status_code})"}

    def get_username_history(self, user_id: int, cursor=None):
        """Read public username history; Roblox may return more than one page."""
        params = {"limit": 100, "sortOrder": "Asc"}
        if cursor:
            params["cursor"] = str(cursor)
        payload, available = self._request_json(
            "GET",
            f"https://users.roblox.com/v1/users/{int(user_id)}/username-history",
            params=params
        )
        if not available:
            return {
                "available": False,
                "names": [],
                "nextCursor": None,
                "message": "Roblox did not return username history for this request."
            }
        rows = payload.get("data", [])
        names = [
            str(row.get("name", "")).strip()
            for row in rows if isinstance(row, dict) and row.get("name")
        ] if isinstance(rows, list) else []
        return {
            "available": True,
            "names": names,
            "nextCursor": payload.get("nextPageCursor"),
            "message": ""
        }

    def get_full_profile(self, query: str):
        user = self._lookup_user(query)
        if "error" in user:
            return user
        
        uid = user["id"]
        
        # Public account details. Missing fields stay unavailable instead of being
        # presented as a confirmed false/zero value.
        profile_data, profile_available = self._request_json(
            "GET", f"https://users.roblox.com/v1/users/{uid}"
        )
        bio = str(profile_data.get("description") or "") if profile_available else ""
        created = str(profile_data.get("created") or "") if profile_available else ""
        created_formatted = created.split("T")[0] if created else "Unknown"
        verified = bool(profile_data.get("hasVerifiedBadge", False)) if profile_available else False
        is_banned = profile_data.get("isBanned") if profile_available else None
        if is_banned is True:
            account_status = "Banned / Terminated"
        elif is_banned is False:
            account_status = "Active"
        else:
            account_status = "Unavailable"

        age_years = None
        if created:
            try:
                created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                age_days = max(0, (datetime.now(timezone.utc) - created_at).days)
                age_years = round(age_days / 365.2425, 1)
            except (TypeError, ValueError):
                age_years = None

        # Counts
        def get_public_count(endpoint):
            payload, available = self._request_json("GET", endpoint)
            value = payload.get("count") if available else None
            return value if isinstance(value, int) and value >= 0 else None

        friends = get_public_count(f"https://friends.roblox.com/v1/users/{uid}/friends/count")
        followers = get_public_count(f"https://friends.roblox.com/v1/users/{uid}/followers/count")
        following = get_public_count(f"https://friends.roblox.com/v1/users/{uid}/followings/count")
        
        # Presence
        presence = self._fetch_presence([uid]).get(str(uid), {})
        presence_available = bool(presence)

        # 2D Avatars
        head_data, _ = self._request_json(
            "GET", f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={uid}&size=150x150&format=Png&isCircular=false"
        )
        head_rows = head_data.get("data", [])
        head_url = head_rows[0].get("imageUrl", "") if head_rows and isinstance(head_rows[0], dict) else ""

        full_data, _ = self._request_json(
            "GET", f"https://thumbnails.roblox.com/v1/users/avatar?userIds={uid}&size=352x352&format=Png&isCircular=false"
        )
        full_rows = full_data.get("data", [])
        full_url = full_rows[0].get("imageUrl", "") if full_rows and isinstance(full_rows[0], dict) else ""

        # Currently Worn Assets
        inventory_api_key_required = False
        worn_data, worn_available = self._request_json(
            "GET", f"https://avatar.roblox.com/v1/users/{uid}/currently-wearing"
        )
        inventory_api_key_required = bool(getattr(self, "_last_request_auth_required", False))
        worn_ids = worn_data.get("assetIds", []) if worn_available else []
        if not isinstance(worn_ids, list):
            worn_ids = []
        
        worn_items = []
        if worn_ids:
            thumb_data, _ = self._request_json(
                "GET", f"https://thumbnails.roblox.com/v1/assets?assetIds={','.join(map(str, worn_ids))}&size=110x110&format=Png"
            )
            thumbs = {
                str(item.get("targetId")): item.get("imageUrl", "")
                for item in thumb_data.get("data", [])
                if isinstance(item, dict) and item.get("state") == "Completed"
            }

            details_data, _ = self._request_json(
                "POST", "https://catalog.roblox.com/v1/catalog/items/details",
                json={"items": [{"itemType": "Asset", "id": aid} for aid in worn_ids]}
            )
            inventory_api_key_required = inventory_api_key_required or bool(getattr(self, "_last_request_auth_required", False))
            details = {
                str(item.get("id")): item
                for item in details_data.get("data", [])
                if isinstance(item, dict) and item.get("id") is not None
            }

            for aid in worn_ids:
                item_details = details.get(str(aid), {})
                worn_items.append({
                    "id": aid,
                    "name": item_details.get("name", f"Asset {aid}"),
                    "thumbnail": thumbs.get(str(aid), ""),
                    "price": item_details.get("price"),
                    "url": f"https://www.roblox.com/catalog/{aid}"
                })

        username_history = self.get_username_history(uid)
        signals = {
            "accountStatus": account_status,
            "accountAgeYears": age_years,
            "verified": verified if profile_available else None,
            "descriptionPresent": bool(bio.strip()) if profile_available else None,
            "usernameHistoryCount": len(username_history["names"]) if username_history["available"] else None,
            "followers": followers
        }

        return {
            "user": user,
            "bio": bio,
            "created": created_formatted,
            "account_age_years": age_years,
            "account_status": {"label": account_status, "isBanned": is_banned},
            "profile_available": profile_available,
            "verified": verified,
            "counts": {"friends": friends, "followers": followers, "following": following},
            "presence": presence,
            "presence_available": presence_available,
            "avatar_head": head_url,
            "avatar_full": full_url,
            "worn_items": worn_items,
            "worn_available": worn_available,
            "inventory_api_key_required": inventory_api_key_required,
            "username_history": username_history,
            "signals": signals
        }

    def get_user_favorites(self, user_id: int):
        r = self._request("GET", f"https://games.roblox.com/v2/users/{user_id}/favorite/games?limit=25")
        if r.status_code != 200:
            return {"error": f"Favorites unavailable (Roblox HTTP {r.status_code})"}

        games = r.json().get("data", [])
        if not games:
            return {"favorites": []}

        universe_ids = [g.get("id") for g in games if g.get("id") is not None]
        thumbs, metrics = {}, {}
        if universe_ids:
            t_res = self._request("GET", f"https://thumbnails.roblox.com/v1/games/icons?universeIds={','.join(map(str, universe_ids))}&returnPolicy=PlaceHolder&size=150x150&format=Png&isCircular=false")
            if t_res.status_code == 200:
                for t in t_res.json().get("data", []):
                    if t.get("state") == "Completed":
                        thumbs[t["targetId"]] = t.get("imageUrl", "")
            # The games endpoint reports current concurrent players on each universe.
            for offset in range(0, len(universe_ids), 50):
                batch = universe_ids[offset:offset + 50]
                g_res = self._request("GET", f"https://games.roblox.com/v1/games?universeIds={','.join(map(str, batch))}")
                if g_res.status_code == 200:
                    for game in g_res.json().get("data", []):
                        metrics[game.get("id")] = game.get("playing")

        out = []
        for g in games:
            uid = g.get("id")
            out.append({
                "id": uid,
                "name": g.get("name", "Unknown Game"),
                "creator": g.get("creator", {}).get("name", "Unknown"),
                "rootPlaceId": g.get("rootPlace", {}).get("id"),
                "thumbnail": thumbs.get(uid, ""),
                "playing": metrics.get(uid),
                "url": f"https://www.roblox.com/games/{g.get('rootPlace', {}).get('id')}"
            })
        return {"favorites": out}

    def get_user_friends(self, user_id: int):
        """
        Friends list is set to Under Construction.
        Stops all calls to blocked friends endpoints to avoid bypassing Roblox security.
        """
        return {
            "under_construction": True,
            "friends": [],
            "message": "Friends list is under construction until official data access is supported without bypassing Roblox security."
        }

    def get_user_groups(self, user_id: int):
        r = self._request("GET", f"https://groups.roblox.com/v2/users/{user_id}/groups/roles")
        if r.status_code != 200:
            return {"error": "Could not fetch groups"}
        
        groups = r.json().get("data", [])
        if not groups:
            return {"groups": []}

        g_ids = [g["group"]["id"] for g in groups]
        icons = {}
        t_res = self._request("GET", f"https://thumbnails.roblox.com/v1/groups/icons?groupIds={','.join(map(str, g_ids))}&size=150x150&format=Png")
        if t_res.status_code == 200:
            for t in t_res.json().get("data", []):
                if t.get("state") == "Completed":
                    icons[t["targetId"]] = t.get("imageUrl", "")

        out = []
        for g in groups:
            grp = g.get("group", {})
            out.append({
                "id": grp.get("id"),
                "name": grp.get("name", "Unknown Group"),
                "memberCount": grp.get("memberCount", 0),
                "role": g.get("role", {}).get("name", "Member"),
                "icon": icons.get(grp.get("id"), ""),
                "url": f"https://www.roblox.com/groups/{grp.get('id')}"
            })
        return {"groups": out}

    def get_user_badges(self, user_id: int):
        """
        Returns official Roblox profile badges (which work reliably).
        Game badges calls are paused and flagged under construction to avoid security issues.
        """
        badges = []
        
        # Official profile badges work cleanly and return full image URLs
        r2 = self._request("GET", f"https://accountinformation.roblox.com/v1/users/{user_id}/roblox-badges")
        if r2.status_code == 200:
            try:
                official = r2.json()
            except ValueError:
                official = []
            for b in official if isinstance(official, list) else []:
                badges.append({
                    "id": b.get("id"),
                    "name": b.get("name", "Roblox Badge"),
                    "description": b.get("description", ""),
                    "type": "Official Profile Badge",
                    "icon": b.get("imageUrl", ""),
                    "awarded": "",
                    "url": ""
                })

        return {
            "badges": badges,
            "game_badges_under_construction": True,
            "message": "Game badges are under construction until a reliable, unblocked API method is supported without bypassing Roblox security."
        }

    # ---------- Enhanced Player Tracker Logic ----------
    def add_tracked(self, query: str):
        user = self._lookup_user(query)
        if "error" in user:
            return user
        if any(int(u["id"]) == int(user["id"]) for u in self.tracked):
            return {"error": f"Player '{user.get('displayName', query)}' is already being tracked"}

        avatar = ""
        try:
            rav = self._request("GET", f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user['id']}&size=150x150&format=Png&isCircular=false")
            if rav.status_code == 200:
                data = rav.json().get("data", [])
                if data and data[0].get("state") == "Completed":
                    avatar = data[0].get("imageUrl", "")
        except Exception:
            pass

        record = {"id": user["id"], "name": user["name"],
                  "displayName": user.get("displayName", user["name"]),
                  "avatar": avatar, "paused": False}
        self.tracked.append(record)
        self._save_tracked()
        return {"ok": True, "user": record}

    def remove_tracked(self, user_id: int):
        self.tracked = [u for u in self.tracked if int(u.get("id", 0)) != int(user_id)]
        self._save_tracked()
        cache = self._load_cache()
        players = cache.get("players", {})
        if isinstance(players, dict):
            players.pop(str(user_id), None)
            cache["players"] = players
            self._save_cache(cache)
        return {"ok": True}

    def toggle_tracked_pause(self, user_id: int, paused):
        for user in self.tracked:
            if int(user.get("id", 0)) == int(user_id):
                user["paused"] = bool(paused)
                self._save_tracked()
                return {"ok": True, "paused": user["paused"]}
        return {"error": "Tracked player not found."}

    def clear_tracked(self):
        self.tracked = []
        self._save_tracked()
        cache = self._load_cache()
        cache["players"] = {}
        cache["refreshedAt"] = None
        self._save_cache(cache)
        return {"ok": True}

    @staticmethod
    def _presence_label(presence):
        try:
            presence_type = int((presence or {}).get("userPresenceType", 0))
        except (TypeError, ValueError):
            presence_type = 0
        return {
            0: "Offline",
            1: "Online",
            2: "In Game",
            3: "In Studio"
        }.get(presence_type, "Unknown")

    def _fetch_presence(self, user_ids):
        """Single presence request path shared by tracker and profile views."""
        ids = [int(uid) for uid in user_ids if str(uid).isdigit()]
        if not ids:
            return {}
        payload, available = self._request_json(
            "POST", "https://presence.roblox.com/v1/presence/users", json={"userIds": ids}
        )
        if not available or not isinstance(payload.get("userPresences"), list):
            return {}
        return {
            str(item.get("userId")): item
            for item in payload["userPresences"]
            if isinstance(item, dict) and item.get("userId") is not None
        }

    def get_presence(self, force=False):
        """Refresh only unpaused players; cache API failures and enforce minimum refresh spacing."""
        cache = self._load_cache()
        cached_players = cache.get("players", {})
        if not isinstance(cached_players, dict):
            cached_players = {}
        now = time.time()
        settings = self.get_tracker_settings()
        min_gap = max(25, min(settings["refreshIntervalSeconds"], 60))
        last_refresh = float(cache.get("lastPresenceRequestAt", 0) or 0)
        rate_limited = bool(last_refresh and now - last_refresh < min_gap)
        queried_at = cache.get("refreshedAt")
        request_error = ""

        active = [u for u in self.tracked if not u.get("paused", False)]
        active_ids = [int(u["id"]) for u in active]
        active_by_id = {str(int(u["id"])): u for u in active}
        transitions = []
        should_query = bool(active_ids) and (force is False or not rate_limited)
        # `force` means try now, but still never violate the backend cooldown.
        should_query = bool(active_ids) and not rate_limited

        if should_query:
            presences = {}
            cache["lastPresenceRequestAt"] = now
            for offset in range(0, len(active_ids), 50):
                batch = active_ids[offset:offset + 50]
                try:
                    presences.update(self._fetch_presence(batch))
                    if not presences:
                        request_error = "Roblox returned no presence records. Showing cached status where available."
                except Exception as e:
                    request_error = f"Presence refresh failed: {e}. Showing cached status where available."

            if presences:
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for uid, presence in presences.items():
                    previous = cached_players.get(uid, {})
                    previous_presence = previous.get("presence", {}) if isinstance(previous, dict) else {}
                    previous_stamp = previous.get("updatedAt") if isinstance(previous, dict) else None
                    previous_is_recent = False
                    if previous_stamp:
                        try:
                            previous_time = datetime.fromisoformat(str(previous_stamp).replace("Z", "+00:00"))
                            previous_is_recent = now - previous_time.timestamp() <= max(
                                120, settings["refreshIntervalSeconds"] * 2
                            )
                        except (TypeError, ValueError):
                            previous_is_recent = False
                    old_status = self._presence_label(previous_presence)
                    new_status = self._presence_label(presence)
                    if previous_is_recent and old_status != new_status and uid in active_by_id:
                        tracked_user = active_by_id[uid]
                        user_avatar = tracked_user.get("avatar") or (previous.get("avatar") if isinstance(previous, dict) else "")
                        transitions.append({
                            "userId": int(uid),
                            "displayName": tracked_user.get("displayName", tracked_user.get("name", "Player")),
                            "name": tracked_user.get("name", ""),
                            "avatar": user_avatar or "",
                            "from": old_status,
                            "to": new_status
                        })
                    cached_players[uid] = {
                        "presence": presence,
                        "updatedAt": stamp,
                        "avatar": previous.get("avatar", "") if isinstance(previous, dict) else ""
                    }
                queried_at = stamp
                cache["refreshedAt"] = stamp
            elif not request_error:
                request_error = "Roblox returned no presence records. Showing cached status where available."
            cache["players"] = cached_players
            self._save_cache(cache)
        elif rate_limited:
            request_error = "Refresh cooldown active; showing the latest cached presence to respect Roblox rate limits."

        is_rate_limited_message = request_error.startswith("Refresh cooldown")

        # Fill missing avatars in one thumbnail call without requesting presence again.
        missing_avatar_ids = [int(u["id"]) for u in self.tracked if not u.get("avatar")]
        if missing_avatar_ids:
            try:
                rav = self._request("GET", f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={','.join(map(str, missing_avatar_ids[:100]))}&size=150x150&format=Png&isCircular=false")
                if rav.status_code == 200:
                    for d in rav.json().get("data", []):
                        if d.get("state") == "Completed":
                            for u in self.tracked:
                                if int(u.get("id", 0)) == int(d.get("targetId", -1)):
                                    u["avatar"] = d.get("imageUrl", "")
                    self._save_tracked()
            except Exception:
                pass

        out = []
        for user in self.tracked:
            entry = cached_players.get(str(user["id"]), {})
            if not isinstance(entry, dict):
                entry = {}
            out.append({
                **user,
                "presence": entry.get("presence", {}),
                "updatedAt": entry.get("updatedAt"),
                "stale": bool(
                    user.get("paused", False)
                    or not entry.get("updatedAt")
                    or (request_error and not is_rate_limited_message)
                    or (entry.get("updatedAt") and (now - datetime.fromisoformat(entry["updatedAt"].replace("Z", "+00:00")).timestamp()) > max(120, settings["refreshIntervalSeconds"] * 2))
                ),
                "refreshError": request_error if not user.get("paused", False) else "Tracking paused for this player.",
                "paused": bool(user.get("paused", False))
            })
        return {"players": out, "refreshedAt": queried_at,
                "refreshIntervalSeconds": settings["refreshIntervalSeconds"],
                "nextRefreshIn": max(0, int(min_gap - (now - last_refresh))) if last_refresh else 0,
                "refreshError": request_error,
                "transitions": transitions}

    # ---------- Server Finder Logic ----------
    def _parse_place_id(self, raw):
        raw = str(raw).strip()
        if raw.isdigit():
            return int(raw)
        m = re.search(r"/games/(\d+)", raw)
        return int(m.group(1)) if m else None

    def get_game_info(self, place_id):
        try:
            r = self._request("GET", f"https://apis.roblox.com/universes/v1/places/{place_id}/universe")
            if r.status_code != 200:
                return None
            universe_id = r.json().get("universeId")
            if not universe_id:
                return None
            
            g = self._request("GET", f"https://games.roblox.com/v1/games?universeIds={universe_id}")
            if g.status_code != 200:
                return None
            data = g.json().get("data", [])
            if not data:
                return None
            
            game = data[0]
            thumb = ""
            t = self._request("GET", f"https://thumbnails.roblox.com/v1/games/icons?universeIds={universe_id}&returnPolicy=PlaceHolder&size=150x150&format=Png&isCircular=false")
            if t.status_code == 200:
                t_data = t.json().get("data", [])
                if t_data and t_data[0].get("state") == "Completed":
                    thumb = t_data[0].get("imageUrl", "")

            return {
                "name": game.get("name", "Unknown"),
                "universeId": universe_id,
                "playing": game.get("playing", 0),
                "visits": game.get("visits", 0),
                "thumbnail": thumb
            }
        except Exception:
            return None

    # ---------- Radar Expansion Services ----------
    def _radar_db(self):
        import sqlite3
        db = sqlite3.connect(str(SCRIPT_DIR / "radar_history.sqlite3"))
        db.execute("CREATE TABLE IF NOT EXISTS game_watch (universe_id INTEGER PRIMARY KEY, place_id INTEGER, name TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.execute("CREATE TABLE IF NOT EXISTS game_snapshots (universe_id INTEGER, playing INTEGER, visits INTEGER, favorites INTEGER, captured_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.commit()
        return db

    def resolve_game(self, raw):
        raw = str(raw or "").strip()
        if not raw: return {"error": "Enter a Place ID, Universe ID, or Roblox experience link."}
        if raw.isdigit():
            # Roblox game links normally use PlaceId. Resolve first; fall back to direct universe ID lookup.
            info = self.get_game_info(int(raw))
            if info: return {"ok": True, **info, "placeId": int(raw), "universeId": info.get("universeId")}
            payload, ok = self._request_json("GET", f"https://games.roblox.com/v1/games?universeIds={raw}")
            if ok and payload.get("data"):
                game = payload["data"][0]
                return {"ok": True, **game, "placeId": int(raw), "universeId": game.get("id")}
            return {"error": "Could not resolve this ID. Try the experience link or its Place ID."}
        m = re.search(r"/games/(\d+)", raw)
        if not m: return {"error": "Could not find a numeric Place ID in that link."}
        result = self.resolve_game(m.group(1))
        if result.get("ok"): result["placeId"] = int(m.group(1))
        return result

    def game_radar_lookup(self, raw):
        game = self.resolve_game(raw)
        if not game.get("ok"): return game
        uid = game.get("universeId") or game.get("id")
        if not uid: return {"error": "Roblox did not return a universe ID."}
        votes, votes_ok = self._request_json("GET", f"https://games.roblox.com/v1/games/votes?universeIds={uid}")
        fav_payload, fav_ok = self._request_json("GET", f"https://games.roblox.com/v1/games/{uid}/favorites/count")
        votes_data = (votes.get("data") or [{}])[0] if votes_ok else {}
        favorite_count = fav_payload.get("favoritesCount") if fav_ok else None
        game.update({"universeId": uid, "votes": votes_data, "favorites": favorite_count})
        try:
            with self._radar_db() as db:
                db.execute("INSERT INTO game_snapshots(universe_id,playing,visits,favorites) VALUES(?,?,?,?)", (int(uid), int(game.get("playing") or 0), int(game.get("visits") or 0), favorite_count))
        except Exception: pass
        return {"ok": True, "game": game}

    def game_watch_add(self, raw):
        result = self.game_radar_lookup(raw)
        if not result.get("ok"): return result
        g = result["game"]
        try:
            with self._radar_db() as db:
                db.execute("INSERT OR REPLACE INTO game_watch(universe_id,place_id,name) VALUES(?,?,?)", (int(g.get("universeId")), int(g.get("placeId") or g.get("rootPlaceId") or 0), str(g.get("name") or "Unknown")))
        except Exception as exc: return {"error": f"Could not save watchlist: {exc}"}
        return {"ok": True, "game": g}

    def game_watch_list(self):
        try:
            with self._radar_db() as db:
                rows = db.execute("SELECT universe_id,place_id,name FROM game_watch ORDER BY name COLLATE NOCASE").fetchall()
                out=[]
                for uid,pid,name in rows:
                    payload, ok = self._request_json("GET", f"https://games.roblox.com/v1/games?universeIds={uid}")
                    g=(payload.get("data") or [{}])[0] if ok else {}
                    if ok:
                        db.execute("INSERT INTO game_snapshots(universe_id,playing,visits,favorites) VALUES(?,?,?,?)", (int(uid), int(g.get("playing") or 0), int(g.get("visits") or 0), g.get("favoritedCount")))
                    hist=db.execute("SELECT playing,visits,favorites,captured_at FROM game_snapshots WHERE universe_id=? ORDER BY captured_at DESC LIMIT 24", (uid,)).fetchall()
                    out.append({"universeId":uid,"placeId":pid,"name":g.get("name",name),"playing":g.get("playing"),"visits":g.get("visits"),"favorites":g.get("favoritedCount"),"history":[{"playing":h[0],"visits":h[1],"favorites":h[2],"time":h[3]} for h in hist]})
                return {"games":out}
        except Exception as exc: return {"games":[], "error":str(exc)}

    def game_watch_remove(self, universe_id):
        try:
            with self._radar_db() as db: db.execute("DELETE FROM game_watch WHERE universe_id=?", (int(universe_id),))
            return {"ok":True}
        except Exception as exc: return {"error":str(exc)}

    def compare_profiles(self, left, right):
        a=self._lookup_user(left); b=self._lookup_user(right)
        if a.get("error") or b.get("error"): return {"error":a.get("error") or b.get("error")}
        out=[]
        uids = [str(a["id"]), str(b["id"])]
        # Fetch real avatar headshots via official thumbnails API
        thumb_map = {}
        t_data, t_ok = self._request_json("GET", f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={','.join(uids)}&size=150x150&format=Png&isCircular=false")
        if t_ok and t_data.get("data"):
            thumb_map = {str(d["targetId"]): d.get("imageUrl", "") for d in t_data["data"]}
        for u in (a,b):
            uid=u["id"]
            u["avatar"] = thumb_map.get(str(uid), "")
            profile,ok=self._request_json("GET",f"https://users.roblox.com/v1/users/{uid}")
            counts={}
            for label,endpoint in (("friends","friends"),("followers","followers"),("following","followings")):
                c,available=self._request_json("GET",f"https://friends.roblox.com/v1/users/{uid}/{endpoint}/count")
                counts[label]=c.get("count") if available else None
            out.append({"user":u,"created":profile.get("created") if ok else None,"verified":profile.get("hasVerifiedBadge") if ok else None,"banned":profile.get("isBanned") if ok else None,"counts":counts})
        return {"profiles":out}

    def bulk_user_lookup(self, raw):
        queries=[q.strip().lstrip('@') for q in re.split(r'[\n,;\t]+', str(raw or '')) if q.strip()]
        queries=queries[:100]
        if not queries: return {"users":[],"error":"Paste usernames or numeric user IDs separated by lines or commas."}
        ids=[]; direct=[]
        for q in queries:
            if q.isdigit(): direct.append(int(q))
            else: ids.append(q)
        result=[]
        for i in range(0,len(ids),100):
            payload,ok=self._request_json("POST","https://users.roblox.com/v1/usernames/users",json={"usernames":ids[i:i+100],"excludeBannedUsers":False})
            if ok: result.extend(payload.get("data",[]))
        for uid in direct:
            payload,ok=self._request_json("GET",f"https://users.roblox.com/v1/users/{uid}")
            if ok and payload.get("id"): result.append(payload)
        return {"users":result,"requested":len(queries),"found":len(result)}

    def group_explorer(self, group_id):
        gid=str(group_id or "").strip()
        if not gid.isdigit(): return {"error":"Enter a numeric group ID."}
        # Roblox v1 groups endpoint is universally supported for all groups
        group,ok=self._request_json("GET",f"https://groups.roblox.com/v1/groups/{gid}")
        if not ok:
            group,ok=self._request_json("GET",f"https://groups.roblox.com/v2/groups/{gid}")
        if not ok: return {"error":f"Group {gid} not found or Roblox API rejected the request."}
        # Fetch group icon thumbnail
        icon_url = ""
        thumb_data, thumb_ok = self._request_json("GET", f"https://thumbnails.roblox.com/v1/groups/icons?groupIds={gid}&size=150x150&format=Png")
        if thumb_ok and thumb_data.get("data"):
            icon_url = thumb_data["data"][0].get("imageUrl", "")
        group["iconUrl"] = icon_url
        roles,roles_ok=self._request_json("GET",f"https://groups.roblox.com/v1/groups/{gid}/roles")
        games,games_ok=self._request_json("GET",f"https://groups.roblox.com/v2/groups/{gid}/gamesV2?accessFilter=2&limit=50")
        if not games_ok:
            games,games_ok=self._request_json("GET",f"https://games.roblox.com/v2/groups/{gid}/games?accessFilter=2&limit=50")
        return {"group":group,"roles":roles.get("roles",[]) if roles_ok else [],"games":games.get("data",[]) if games_ok else [],"rolesAvailable":roles_ok,"gamesAvailable":games_ok}

    def catalog_search(self, keyword, limit=30):
        q=str(keyword or "").strip()
        if not q: return {"items":[],"error":"Enter an item name to search."}
        limit=max(10,min(40,int(limit or 30)))
        # Search catalog details endpoint which returns price, creatorName, favoriteCount, etc.
        url=f"https://catalog.roblox.com/v1/search/items/details?category=All&keyword={requests.utils.quote(q)}&limit={limit}"
        payload,ok=self._request_json("GET",url)
        if not ok:
            url=f"https://catalog.roblox.com/v1/search/items/details?keyword={requests.utils.quote(q)}&limit={limit}"
            payload,ok=self._request_json("GET",url)
        items = payload.get("data", []) if ok else []
        if items:
            # Batch fetch high-resolution thumbnails for all items
            asset_ids = [str(i["id"]) for i in items if i.get("id")]
            if asset_ids:
                t_url = f"https://thumbnails.roblox.com/v1/assets?assetIds={','.join(asset_ids[:50])}&size=150x150&format=Png"
                t_payload, t_ok = self._request_json("GET", t_url)
                if t_ok and t_payload.get("data"):
                    thumb_map = {str(d["targetId"]): d.get("imageUrl", "") for d in t_payload["data"]}
                    for i in items:
                        i["thumbnail"] = thumb_map.get(str(i.get("id")), "")
        return {"items":items,"error":None if ok else "Catalog search unavailable for this query or endpoint."}

    def fetch_servers(self, place_id: int, cursor: str = None):
        url = f"https://games.roblox.com/v1/games/{place_id}/servers/0?sortOrder=Desc&excludeFullGames=false&limit=100"
        if cursor:
            url += f"&cursor={cursor}"
        r = self._request("GET", url)
        if r.status_code != 200:
            return {"error": f"Failed to fetch servers (HTTP {r.status_code})"}
        return r.json()

    def launch_roblox(self, place_id: int, job_id: str = None):
        uri = f"roblox://experiences/start?placeId={place_id}"
        if job_id:
            uri += f"&gameInstanceId={job_id}"
        webbrowser.open(uri)
        return {"ok": True}

HTML_UI = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Roblox Radar - GlobalStats Edition</title>
<style>
  @property --angle { syntax: "<angle>"; initial-value: 0deg; inherits: false; }

  :root {
    --bg: #111119;
    --panel-bg: #181824;
    --card-bg: #202030;
    --card-hover: #28283d;
    --border: rgba(255,255,255,.06);
    --border-bright: rgba(255,255,255,.12);
    --text: #e8e8f2;
    --muted: #8d8da8;
    --accent: #7b61ff;
    --accent-hover: #947cff;
    --accent-glow: rgba(123, 97, 255, 0.25);
    --cyan: #00d2ff;
    --cyan-glow: rgba(0, 210, 255, 0.2);
    --green: #2ecc71;
    --green-glow: rgba(46, 204, 113, 0.25);
    --red: #ff4757;
    --yellow: #f1c40f;
    --blue: #3498db;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    /* Offline-safe: use Inter when installed, then platform-native UI fonts. */
    font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 13px;
    font-feature-settings: "cv11", "ss01";
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  body::before {
    content: "";
    position: fixed; inset: 0; z-index: -2;
    background-image:
      linear-gradient(rgba(123,97,255,.05) 1px, transparent 1px),
      linear-gradient(90deg, rgba(123,97,255,.05) 1px, transparent 1px);
    background-size: 40px 40px;
    mask-image: radial-gradient(circle at center, #000 30%, transparent 80%);
    -webkit-mask-image: radial-gradient(circle at center, #000 30%, transparent 80%);
    pointer-events: none;
  }
  body::after {
    content: "";
    position: fixed; inset: -20%; z-index: -1;
    background:
      radial-gradient(circle at 20% 30%, rgba(123,97,255,.18), transparent 45%),
      radial-gradient(circle at 80% 70%, rgba(0,210,255,.14), transparent 45%);
    animation: drift 20s ease-in-out infinite alternate;
    pointer-events: none;
  }

  /* Film grain + vignette overlay */
  .grain-overlay {
    position: fixed; inset: 0; z-index: 9998;
    background-image:
      radial-gradient(ellipse at center, transparent 60%, rgba(0,0,0,.65) 100%),
      url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
    opacity: .04;
    mix-blend-mode: overlay;
    pointer-events: none;
    animation: grain-shift 0.15s steps(1) infinite;
  }
  @keyframes grain-shift {
    0%   { background-position: 0 0; }
    25%  { background-position: -40px -20px; }
    50%  { background-position: 20px 40px; }
    75%  { background-position: -20px 20px; }
    100% { background-position: 40px -40px; }
  }
  @keyframes drift {
    0%   { transform: translate(0, 0) scale(1); }
    100% { transform: translate(6%, -4%) scale(1.1); }
  }

  /* Fullscreen Splash Screen */
  .splash-overlay {
    position: fixed;
    inset: 0;
    z-index: 99999;
    background: radial-gradient(circle at center, #1b1b2a 0%, #0d0d14 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    transition: opacity 0.6s ease, transform 0.6s ease, filter 0.6s ease, visibility 0.6s ease;
  }
  .splash-overlay.hidden {
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
    transform: scale(1.15);
    filter: blur(12px);
  }

  /* Splash radar */
  .splash-radar-wrap {
    position: relative;
    width: 170px;
    height: 170px;
    margin-bottom: 22px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    filter: drop-shadow(0 0 24px rgba(0,210,255,.35));
  }
  .splash-radar {
    width: 170px;
    height: 170px;
    border-radius: 50%;
    position: relative;
    overflow: hidden;
    background: radial-gradient(circle at center, rgba(0,210,255,.08) 0%, rgba(18,18,30,.92) 75%);
    border: 1.5px solid rgba(0,210,255,.4);
    box-shadow: inset 0 0 20px rgba(0,210,255,.15);
  }
  .splash-radar-sweep {
    position: absolute;
    inset: 0;
    border-radius: 50%;
    background: conic-gradient(from 0deg,
      transparent 0deg,
      transparent 260deg,
      rgba(0,210,255,.15) 300deg,
      rgba(0,210,255,.65) 360deg);
    animation: sweep 2.2s linear infinite;
  }
  .splash-radar-rings {
    position: absolute;
    inset: 0;
    border-radius: 50%;
  }
  .splash-radar-rings::before,
  .splash-radar-rings::after {
    content: "";
    position: absolute;
    border-radius: 50%;
    border: 1px solid rgba(0,210,255,.22);
  }
  .splash-radar-rings::before { inset: 22%; }
  .splash-radar-rings::after  { inset: 48%; }
  .splash-radar-outer {
    position: absolute;
    inset: -6px;
    border-radius: 50%;
    border: 1px dashed rgba(0,210,255,.25);
    animation: rotate-reverse 20s linear infinite;
  }
  .splash-radar-crosshair-h {
    position: absolute;
    top: 50%;
    left: 0;
    right: 0;
    height: 1px;
    background: rgba(0,210,255,.2);
    transform: translateY(-50%);
  }
  .splash-radar-crosshair-v {
    position: absolute;
    left: 50%;
    top: 0;
    bottom: 0;
    width: 1px;
    background: rgba(0,210,255,.2);
    transform: translateX(-50%);
  }
  .splash-radar-center {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: 32px;
    height: 32px;
    border-radius: 50%;
    background: linear-gradient(135deg, rgba(124,58,237,.8) 0%, rgba(0,210,255,.9) 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 0 16px rgba(0,210,255,.7);
    border: 1px solid rgba(255,255,255,.4);
    z-index: 2;
  }
  .splash-radar-icon {
    font-size: 14px;
    font-weight: 900;
    color: #fff;
    letter-spacing: -0.5px;
  }
  .splash-blip {
    position: absolute;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #00d2ff;
    box-shadow: 0 0 10px #00d2ff;
    z-index: 1;
  }
  .blip-1 { animation: blip-pulse 2.2s ease-in-out infinite; }
  .blip-2 { animation: blip-pulse 2.2s ease-in-out infinite 0.7s; }
  .blip-3 { animation: blip-pulse 2.2s ease-in-out infinite 1.4s; }
  @keyframes blip-pulse {
    0%, 100% { opacity: .2; transform: scale(.7); }
    50%       { opacity: 1; transform: scale(1.4); box-shadow: 0 0 14px #00d2ff, 0 0 20px rgba(0,210,255,.6); }
  }
  @keyframes sweep { to { transform: rotate(360deg); } }
  @keyframes rotate-reverse { to { transform: rotate(-360deg); } }
  .splash-logo-label {
    font-size: 22px;
    font-weight: 800;
    letter-spacing: -0.01em;
    background: linear-gradient(135deg, #fff 0%, var(--cyan) 50%, var(--accent) 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 4px;
  }
  .splash-card {
    width: 480px;
    max-width: 90vw;
    background: rgba(24, 24, 38, 0.95);
    border: 1px solid rgba(255,255,255,.1);
    border-radius: 20px;
    padding: 32px 28px 24px;
    box-shadow: 0 20px 50px rgba(0,0,0,0.6), 0 0 60px var(--accent-glow);
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    backdrop-filter: blur(16px);
  }
  .splash-title {
    font-size: 22px;
    font-weight: 800;
    letter-spacing: -0.01em;
    background: linear-gradient(135deg, #fff 0%, var(--cyan) 50%, var(--accent) 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 4px;
  }
  .splash-tagline {
    font-size: 13px;
    color: var(--cyan);
    font-weight: 600;
    letter-spacing: 0.5px;
    margin-bottom: 24px;
  }
  .splash-bar-wrap {
    width: 100%;
    height: 5px;
    background: rgba(255,255,255,.06);
    border-radius: 999px;
    overflow: hidden;
    margin-bottom: 12px;
  }
  .splash-bar-fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--accent) 0%, var(--cyan) 100%);
    box-shadow: 0 0 10px var(--cyan);
    border-radius: 999px;
    transition: width 0.25s ease;
  }
  .splash-status {
    font-size: 11px;
    color: var(--muted);
    font-weight: 500;
    min-height: 16px;
  }

  /* Top Navigation */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 24px;
    background: var(--panel-bg);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    position: relative;
    z-index: 10;
  }
  .brand-group {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .brand-logo {
    width: 28px;
    height: 28px;
    border-radius: 8px;
    background: linear-gradient(135deg, var(--accent) 0%, var(--cyan) 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    font-size: 14px;
    color: #fff;
    box-shadow: 0 0 12px var(--accent-glow);
    animation: logo-glow 3s ease-in-out infinite;
  }
  @keyframes logo-glow {
    0%, 100% { box-shadow: 0 0 10px var(--accent-glow); }
    50%       { box-shadow: 0 0 20px var(--accent-glow), 0 0 36px var(--cyan-glow); }
  }
  .brand-title {
    font-size: 15px;
    font-weight: 800;
    letter-spacing: -0.01em;
  }
  .brand-tag {
    font-size: 9px;
    padding: 2px 5px;
    background: var(--card-bg);
    color: var(--cyan);
    border: 1px solid rgba(255,255,255,.1);
    border-radius: 4px;
    font-weight: 600;
    letter-spacing: 0.5px;
    text-transform: uppercase;
  }
  .live-pill {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    background: rgba(46,204,113,.08);
    border: 1px solid rgba(46,204,113,.25);
    border-radius: 999px;
    font-size: 11px;
    font-weight: 700;
    color: var(--green);
    white-space: nowrap;
  }
  .live-pill .live-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 6px var(--green);
    animation: livePulseGreen 1.5s infinite;
    flex-shrink: 0;
  }
  .tabs {
    display: flex;
    gap: 6px;
  }
  .tab {
    background: transparent;
    border: none;
    color: var(--muted);
    padding: 7px 16px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    border-radius: 10px;
    transition: all 0.2s;
    position: relative;
    z-index: 0;
  }
  .tab:hover {
    color: var(--text);
    background: var(--card-bg);
  }
  .tab.active {
    background: var(--card-bg);
    color: #fff;
    box-shadow: 0 4px 12px var(--accent-glow);
  }
  .tab.active::before {
    content: "";
    position: absolute; inset: -1px;
    background: conic-gradient(from var(--angle),
      var(--accent), var(--cyan), var(--accent));
    border-radius: inherit;
    z-index: -1;
    animation: spin-angle 3s linear infinite;
  }
  @keyframes spin-angle { to { --angle: 360deg; } }
  .header-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  /* Searchable Language Picker Dropdown (removed) */
  .icon-btn {
    background: var(--card-bg);
    border: 1px solid var(--border);
    color: var(--muted);
    width: 32px;
    height: 32px;
    border-radius: 10px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    transition: all 0.2s;
  }
  .icon-btn:hover {
    color: var(--text);
    border-color: rgba(255,255,255,.18);
    background: var(--card-hover);
  }

  /* Main Workspace */
  main {
    flex: 1;
    overflow-y: auto;
    padding: 24px;
  }
  .panel {
    display: none;
    max-width: 1100px;
    margin: 0 auto;
  }
  .panel.active {
    display: block;
    animation: slideIn 0.25s cubic-bezier(.2,.8,.2,1);
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @keyframes slideIn {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  .panel-title {
    font-size: 20px;
    font-weight: 800;
    letter-spacing: -0.01em;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 10px;
  }

  /* Forms & Buttons */
  .form-group {
    margin-bottom: 16px;
  }
  label {
    display: block;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.2px;
    color: var(--muted);
    margin-bottom: 6px;
    text-transform: uppercase;
  }
  .row {
    display: flex;
    gap: 10px;
  }
  input[type="text"], input[type="number"] {
    flex: 1;
    background: var(--panel-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 14px;
    color: var(--text);
    font-size: 13px;
    outline: none;
    transition: border 0.2s;
    font-family: inherit;
  }
  input[type="text"]:focus, input[type="number"]:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-glow);
  }
  .btn {
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 10px;
    padding: 10px 18px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    position: relative;
    overflow: hidden;
    font-family: inherit;
  }
  .btn:hover {
    background: var(--accent-hover);
    box-shadow: 0 4px 12px var(--accent-glow);
  }
  .btn .ripple {
    position: absolute;
    border-radius: 50%;
    background: rgba(255,255,255,.35);
    transform: scale(0);
    animation: ripple-anim 0.55s linear;
    pointer-events: none;
  }
  @keyframes ripple-anim {
    to { transform: scale(4); opacity: 0; }
  }
  .btn.secondary {
    background: var(--card-bg);
    color: var(--text);
    border: 1px solid var(--border);
  }
  .btn.secondary:hover {
    background: var(--card-hover);
    border-color: rgba(255,255,255,.2);
  }
  .btn.danger {
    background: var(--red);
  }
  .btn.danger:hover {
    background: #ff6b81;
    box-shadow: 0 4px 12px rgba(255, 71, 87, 0.3);
  }
  .btn.small {
    padding: 6px 12px;
    font-size: 12px;
  }
  .error-msg {
    color: var(--red);
    font-size: 12px;
    font-weight: 500;
  }
  .empty-text {
    color: var(--muted);
    font-size: 13px;
    text-align: center;
    padding: 40px;
  }

  /* Empty state illustrations */
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 14px;
    padding: 56px 24px;
    text-align: center;
  }
  .empty-state-icon {
    font-size: 48px;
    line-height: 1;
    filter: drop-shadow(0 0 16px rgba(123,97,255,.4));
    animation: float 3s ease-in-out infinite;
  }
  @keyframes float {
    0%, 100% { transform: translateY(0); }
    50%       { transform: translateY(-6px); }
  }
  .empty-state-title {
    font-size: 17px;
    font-weight: 800;
    letter-spacing: -0.01em;
    color: var(--text);
  }
  .empty-state-desc {
    font-size: 13px;
    color: var(--muted);
    max-width: 340px;
    line-height: 1.6;
  }

  /* Under Construction Cards */
  .construction-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 32px 24px;
    text-align: center;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    margin: 12px 0;
  }
  .construction-icon {
    font-size: 36px;
    line-height: 1;
  }
  .construction-title {
    font-size: 16px;
    font-weight: 700;
    color: var(--text);
  }
  .construction-desc {
    font-size: 13px;
    color: var(--muted);
    max-width: 520px;
    line-height: 1.5;
  }
  .construction-badge {
    display: inline-block;
    padding: 3px 8px;
    background: rgba(241, 196, 15, 0.15);
    color: var(--yellow);
    border: 1px solid rgba(241, 196, 15, 0.3);
    border-radius: 6px;
    font-size: 11px;
    font-weight: 700;
  }
  .construction-banner {
    display: flex;
    align-items: center;
    gap: 12px;
    background: rgba(241, 196, 15, 0.08);
    border: 1px solid rgba(241, 196, 15, 0.25);
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 16px;
    color: #e0d080;
    font-size: 12px;
    line-height: 1.4;
  }

  /* Player Profile UI */
  .pf-header {
    display: flex;
    gap: 20px;
    background: var(--panel-bg);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 22px;
    margin-bottom: 20px;
    align-items: center;
    position: relative;
    transform-style: preserve-3d;
    transition: transform .25s cubic-bezier(.2,.8,.2,1), box-shadow .25s, border-color .25s;
    overflow: hidden;
  }
  .pf-header:hover {
    border-color: rgba(123,97,255,.35);
    box-shadow: 0 16px 36px rgba(0,0,0,.4), 0 0 20px var(--accent-glow);
    transform: perspective(900px) rotateX(1.5deg) rotateY(-1.5deg) translateY(-2px);
  }
  .pf-header::after {
    content: ""; position: absolute; inset: 0; border-radius: inherit;
    background: radial-gradient(500px circle at var(--mx,50%) var(--my,50%),
                rgba(0,210,255,.08), transparent 60%);
    opacity: 0; transition: opacity .3s; pointer-events: none;
  }
  .pf-header:hover::after { opacity: 1; }
  .pf-avatar-wrap {
    position: relative;
    width: 110px; height: 110px;
    flex-shrink: 0;
    border-radius: 14px;
    overflow: hidden;
  }
  .pf-avatar-full {
    width: 100%; height: 100%;
    border-radius: 14px;
    background: var(--card-bg);
    object-fit: cover;
    border: 1px solid var(--border);
    transition: transform .3s ease;
  }
  .pf-avatar-wrap:hover .pf-avatar-full {
    transform: scale(1.04);
  }
  .pf-avatar-scanline {
    position: absolute;
    inset: 0;
    background: linear-gradient(rgba(0,210,255,0) 50%, rgba(0,210,255,0.2) 50%),
                linear-gradient(90deg, rgba(255,0,0,0.06), rgba(0,255,0,0.02), rgba(0,0,255,0.06));
    background-size: 100% 4px, 6px 100%;
    opacity: 0;
    pointer-events: none;
    transition: opacity .3s;
    border-radius: 14px;
  }
  .pf-avatar-wrap:hover .pf-avatar-scanline {
    opacity: 1;
    animation: scanmove 2s linear infinite;
  }
  @keyframes scanmove {
    0% { background-position: 0 0, 0 0; }
    100% { background-position: 0 100%, 0 0; }
  }

  /* Verified badge animated shine */
  .verified-badge {
    display: inline-flex;
    align-items: center;
    position: relative;
    overflow: hidden;
    color: var(--cyan);
    margin-left: 4px;
    vertical-align: middle;
    border-radius: 50%;
  }
  .verified-badge::after {
    content: "";
    position: absolute;
    top: 0; left: -100%;
    width: 60%; height: 100%;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,0.7), transparent);
    transform: skewX(-20deg);
    animation: badge-shine 4s infinite;
  }
  @keyframes badge-shine {
    0%, 75% { left: -100%; }
    100%    { left: 200%; }
  }

  /* Worn items catalog styling with hover reveal */
  .worn-item-card {
    position: relative;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    transition: all .25s ease;
    display: flex;
    flex-direction: column;
  }
  .worn-item-card:hover {
    border-color: var(--accent);
    transform: translateY(-3px);
    box-shadow: 0 12px 24px rgba(0,0,0,.4), 0 0 12px var(--accent-glow);
  }
  .worn-item-thumb-wrap {
    position: relative;
    width: 100%;
    aspect-ratio: 1;
    overflow: hidden;
    background: #111119;
  }
  .worn-item-thumb {
    width: 100%; height: 100%;
    object-fit: cover;
    transition: transform .35s ease;
  }
  .worn-item-card:hover .worn-item-thumb {
    transform: scale(1.08);
  }
  .worn-price-pill {
    position: absolute;
    top: 8px; right: 8px;
    background: rgba(17,17,25,.88);
    border: 1px solid rgba(255,255,255,.15);
    padding: 2px 7px;
    border-radius: 999px;
    font-size: 10px;
    font-weight: 700;
    color: var(--cyan);
    backdrop-filter: blur(8px);
    transition: transform .25s ease, opacity .25s ease;
    transform: translateY(-8px);
    opacity: 0;
    z-index: 2;
  }
  .worn-item-card:hover .worn-price-pill {
    transform: translateY(0);
    opacity: 1;
  }
  .worn-scrim {
    position: absolute;
    inset: 0;
    background: linear-gradient(to top, rgba(17,17,25,.95) 0%, rgba(17,17,25,.4) 40%, transparent 100%);
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    padding: 10px;
    opacity: 0;
    transform: translateY(8px);
    transition: all .25s ease;
  }
  .worn-item-card:hover .worn-scrim {
    opacity: 1;
    transform: translateY(0);
  }
  .worn-title {
    font-size: 11px;
    font-weight: 700;
    color: #fff;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Username history git log timeline */
  .timeline-container {
    position: relative;
    padding-left: 20px;
    margin: 16px 0;
  }
  .timeline-container::before {
    content: "";
    position: absolute;
    top: 8px; bottom: 8px; left: 5px;
    width: 2px;
    background: rgba(255,255,255,.1);
  }
  .timeline-item {
    position: relative;
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 12px;
  }
  .timeline-item::before {
    content: "";
    position: absolute;
    left: -19px;
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 6px var(--accent);
  }
  .timeline-name {
    font-weight: 600;
    color: var(--text);
    background: var(--card-bg);
    border: 1px solid var(--border);
    padding: 5px 10px;
    border-radius: 8px;
  }
  .timeline-tag {
    font-size: 11px;
    color: var(--muted);
  }

  /* Mini game card inside player card */
  .pc-game-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 8px;
    background: rgba(52,152,219,.12);
    border: 1px solid rgba(52,152,219,.3);
    border-radius: 6px;
    color: var(--blue);
    font-size: 11px;
    font-weight: 600;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    margin-top: 2px;
  }
  .pf-details {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .pf-name {
    font-size: 20px;
    font-weight: 800;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .pf-username {
    font-size: 13px;
    color: var(--muted);
  }
  .pf-bio {
    font-size: 12px;
    color: var(--muted);
    font-style: italic;
    line-height: 1.4;
    margin: 4px 0;
  }
  .pf-stats {
    display: flex;
    gap: 16px;
    font-size: 12px;
    color: var(--muted);
    margin-top: 4px;
    flex-wrap: wrap;
  }
  .pf-stats span strong {
    color: var(--text);
  }
  .pf-actions {
    display: flex;
    gap: 8px;
    margin-top: 6px;
    flex-wrap: wrap;
  }
  .panel-intro {
    color: var(--muted);
    font-size: 13px;
    line-height: 1.55;
    max-width: 760px;
    margin: -10px 0 20px;
  }
  .pf-status-row {
    display: flex;
    align-items: center;
    gap: 9px;
    flex-wrap: wrap;
    margin-top: 2px;
  }
  .status-chip {
    display: inline-flex;
    align-items: center;
    padding: 4px 9px;
    border: 1px solid var(--border-bright);
    border-radius: 999px;
    background: var(--card-bg);
    color: var(--muted);
    font-size: 11px;
    font-weight: 700;
  }
  .status-chip.active {
    color: var(--green);
    border-color: rgba(46,204,113,.35);
    background: rgba(46,204,113,.08);
  }
  .status-chip.terminated {
    color: var(--red);
    border-color: rgba(255,71,87,.4);
    background: rgba(255,71,87,.08);
  }
  .status-chip.unavailable {
    color: var(--yellow);
    border-color: rgba(241,196,15,.35);
    background: rgba(241,196,15,.06);
  }
  .profile-signal-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
    gap: 10px;
    margin: 0 0 10px;
  }
  .profile-signal-card {
    min-width: 0;
    padding: 13px 14px;
    background: linear-gradient(145deg, var(--panel-bg), var(--card-bg));
    border: 1px solid var(--border);
    border-radius: 10px;
  }
  .profile-signal-label {
    color: var(--muted);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .65px;
    text-transform: uppercase;
    margin-bottom: 6px;
  }
  .profile-signal-value {
    color: var(--text);
    font-size: 15px;
    font-weight: 750;
    overflow-wrap: anywhere;
  }
  .profile-signal-detail {
    color: var(--muted);
    font-size: 11px;
    line-height: 1.4;
    margin-top: 5px;
  }
  .profile-note {
    color: var(--muted);
    font-size: 11px;
    line-height: 1.5;
    padding: 10px 12px;
    margin: 0 0 18px;
    border-left: 2px solid var(--cyan);
    background: rgba(0,210,255,.045);
    border-radius: 0 7px 7px 0;
  }
  .username-history-list {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
    gap: 8px;
    list-style: none;
    margin: 12px 0;
  }
  .username-history-item {
    padding: 10px 12px;
    color: var(--text);
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow-wrap: anywhere;
  }
  .history-actions {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    margin-top: 12px;
  }
  .item-card-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }
  .notification-restart-banner {
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    flex-wrap: wrap;
    padding: 14px 16px;
    margin: 0 0 16px;
    background: rgba(0, 210, 255, 0.075);
    border: 1px solid rgba(0, 210, 255, 0.35);
    border-radius: 10px;
  }
  .notification-restart-copy {
    display: flex;
    flex: 1 1 320px;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }
  .notification-restart-title {
    color: var(--text);
    font-size: 13px;
    font-weight: 750;
  }
  .notification-restart-description {
    color: var(--muted);
    font-size: 11px;
    line-height: 1.45;
  }
  .notification-restart-banner .btn {
    flex: 0 0 auto;
    white-space: nowrap;
  }
  .tracker-notification-row {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    padding: 10px 12px;
    margin: -4px 0 16px;
    background: var(--panel-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
  }
  .tracker-notification-toggle {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    margin: 0;
    color: var(--text);
    font-size: 12px;
    font-weight: 650;
    letter-spacing: 0;
    text-transform: none;
    cursor: pointer;
  }
  .tracker-notification-toggle input {
    accent-color: var(--cyan);
    width: 15px;
    height: 15px;
  }
  .notification-note {
    color: var(--muted);
    font-size: 11px;
    line-height: 1.4;
    flex: 1 1 240px;
  }
  @media (max-width: 850px) {
    header { flex-wrap: wrap; gap: 10px; padding: 12px 16px; }
    .tabs { order: 3; width: 100%; overflow-x: auto; }
    .tab { white-space: nowrap; }
    main { padding: 18px; }
    .pf-header { align-items: flex-start; }
  }
  @media (max-width: 620px) {
    .pf-header { flex-direction: column; }
    .pf-avatar-full { width: 88px; height: 88px; }
    .pf-actions { width: 100%; }
    .pf-actions .btn { flex: 1 1 auto; }
    .row { flex-direction: column; }
    .tracker-grid { grid-template-columns: 1fr; }
    .sf-item, .sf-info { align-items: flex-start; flex-direction: column; }
  }

  /* Profile Sub-tabs */
  .sub-tabs {
    display: flex;
    gap: 6px;
    margin-bottom: 16px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 12px;
  }
  .sub-tab {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 600;
    border-radius: 20px;
    cursor: pointer;
    transition: all 0.2s;
    font-family: inherit;
  }
  .sub-tab:hover {
    color: var(--text);
    border-color: rgba(255,255,255,.2);
    background: var(--card-bg);
  }
  .sub-tab.active {
    background: var(--card-bg);
    border-color: var(--accent);
    color: #fff;
  }
  .sub-panel {
    display: none;
  }
  .sub-panel.active {
    display: block;
    animation: slideIn .25s cubic-bezier(.2,.8,.2,1);
  }

  /* Grids for Items/Favorites/Badges */
  .grid-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
    gap: 12px;
  }
  .grid-badges {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 14px;
  }
  .fav-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    transition: all 0.2s;
  }
  .fav-card:hover {
    border-color: var(--accent);
    transform: translateY(-2px);
    background: var(--card-hover);
  }
  .fav-card img {
    width: 100%;
    aspect-ratio: 1;
    border-radius: 6px;
    background: #111;
    object-fit: cover;
  }
  .fav-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .fav-name {
    font-weight: 600;
    font-size: 12px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .fav-meta {
    font-size: 11px;
    color: var(--muted);
  }
  .game-playing {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    margin-top: 6px;
    font-size: 11px;
    color: var(--text);
  }
  .game-live-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
    flex: 0 0 auto;
  }
  .game-live-dot.active {
    background: var(--green);
    box-shadow: 0 0 0 0 rgba(46,204,113,.6);
    animation: livePulseGreen 1.5s infinite;
  }
  .game-live-dot.zero {
    background: var(--red);
    box-shadow: 0 0 0 0 rgba(255,71,87,.55);
    animation: livePulseRed 1.5s infinite;
  }
  @keyframes livePulseGreen {
    0% { box-shadow: 0 0 0 0 rgba(46,204,113,.55); }
    70% { box-shadow: 0 0 0 7px rgba(46,204,113,0); }
    100% { box-shadow: 0 0 0 0 rgba(46,204,113,0); }
  }
  @keyframes livePulseRed {
    0% { box-shadow: 0 0 0 0 rgba(255,71,87,.55); }
    70% { box-shadow: 0 0 0 7px rgba(255,71,87,0); }
    100% { box-shadow: 0 0 0 0 rgba(255,71,87,0); }
  }
  .tracker-settings-row {
    display:flex; align-items:center; flex-wrap:wrap; gap:10px;
    padding:10px 12px; margin-bottom:14px; background:var(--panel-bg);
    border:1px solid var(--border); border-radius:8px;
  }
  .tracker-settings-row select {
    background:var(--card-bg); color:var(--text); border:1px solid var(--border);
    padding:7px 9px; border-radius:7px;
  }
  .pc-freshness { font-size:10px; color:var(--muted); margin-top:5px; }
  .pc-freshness.stale { color:var(--yellow); }
  .pc-paused-badge { font-size:10px; color:var(--yellow); border:1px solid rgba(241,196,15,.3); border-radius:4px; padding:2px 6px; }
  .pc-pause-btn { min-width:72px; }

  /* No-filter-match crosshair */
  .filter-no-results {
    grid-column: 1/-1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 14px;
    padding: 56px 24px;
    text-align: center;
  }
  .crosshair-icon {
    position: relative;
    width: 56px; height: 56px;
    animation: ch-spin 4s linear infinite;
  }
  .crosshair-icon::before,
  .crosshair-icon::after {
    content: "";
    position: absolute;
    background: var(--cyan);
    border-radius: 2px;
    opacity: .7;
  }
  .crosshair-icon::before { width: 2px; height: 56px; left: 50%; top: 0; transform: translateX(-50%); }
  .crosshair-icon::after  { width: 56px; height: 2px; top: 50%; left: 0; transform: translateY(-50%); }
  .crosshair-ring {
    position: absolute; inset: 6px;
    border-radius: 50%;
    border: 2px solid var(--cyan);
    opacity: .6;
    animation: ch-pulse 2s ease-in-out infinite;
  }
  @keyframes ch-spin  { to { transform: rotate(360deg); } }
  @keyframes ch-pulse { 0%,100% { transform: scale(.9); opacity:.4; } 50% { transform: scale(1.1); opacity:.9; } }
  .filter-no-results-title { font-size:15px; font-weight:700; color:var(--text); }
  .filter-no-results-desc  { font-size:12px; color:var(--muted); }

  .badge-image-fallback {
    width: 100%;
    aspect-ratio: 1;
    border-radius: 6px;
    background: #1a1a26;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--muted);
    font-size: 10px;
    text-align: center;
    border: 1px dashed var(--border);
  }

  /* ========================================================= */
  /* ENHANCED PLAYER TRACKER STYLES                            */
  /* ========================================================= */
  .tracker-header {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
  }
  .tracker-summary-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 20px;
  }
  .stat-pill {
    display: flex;
    align-items: center;
    gap: 8px;
    background: var(--panel-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 14px;
    font-size: 12px;
    font-weight: 600;
  }
  .stat-pill .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }
  .dot.online { background: var(--green); box-shadow: 0 0 8px var(--green-glow); }
  .dot.ingame { background: var(--blue); box-shadow: 0 0 8px rgba(52, 152, 219, 0.4); }
  .dot.instudio { background: var(--yellow); box-shadow: 0 0 8px rgba(241, 196, 15, 0.4); }
  .dot.offline { background: var(--muted); }
  .dot.total { background: var(--cyan); box-shadow: 0 0 8px var(--cyan-glow); }

  .stat-pill .count {
    background: var(--card-bg);
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 11px;
    color: var(--text);
    border: 1px solid var(--border);
  }

  .tracker-controls {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .tracker-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 16px;
  }

  .player-card {
    background: var(--panel-bg);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    position: relative;
    transition: transform .25s cubic-bezier(.2,.8,.2,1), box-shadow .25s, border-color .25s;
    transform-style: preserve-3d;
    overflow: hidden;
  }
  .player-card:hover {
    border-color: rgba(123,97,255,.4);
    background: var(--card-bg);
    transform: perspective(900px) rotateX(2deg) rotateY(-2deg) translateY(-4px);
    box-shadow:
      0 20px 40px rgba(0,0,0,.45),
      0 0 0 1px var(--accent),
      0 0 24px var(--accent-glow);
  }
  .player-card::after {
    content: ""; position: absolute; inset: 0; border-radius: inherit;
    background: radial-gradient(400px circle at var(--mx,50%) var(--my,50%),
                rgba(123,97,255,.12), transparent 60%);
    opacity: 0; transition: opacity .3s; pointer-events: none;
  }
  .player-card:hover::after { opacity: 1; }

  /* Status flash animations */
  @keyframes flash-online {
    0%   { box-shadow: 0 0 0 0 rgba(46,204,113,.8), 0 20px 40px rgba(0,0,0,.45); }
    100% { box-shadow: 0 0 0 14px rgba(46,204,113,0), 0 20px 40px rgba(0,0,0,.45); }
  }
  @keyframes flash-ingame {
    0%   { box-shadow: 0 0 0 0 rgba(52,152,219,.8); }
    100% { box-shadow: 0 0 0 14px rgba(52,152,219,0); }
  }
  @keyframes flash-offline {
    0%   { box-shadow: 0 0 0 0 rgba(141,141,168,.6); }
    100% { box-shadow: 0 0 0 14px rgba(141,141,168,0); }
  }
  .flash-online  { animation: flash-online .7s ease-out; }
  .flash-ingame  { animation: flash-ingame .7s ease-out; }
  .flash-offline { animation: flash-offline .7s ease-out; }

  /* Bloom particles */
  .bloom-particle {
    position: absolute;
    width: 5px; height: 5px;
    border-radius: 50%;
    pointer-events: none;
    animation: bloom 900ms ease-out forwards;
    z-index: 20;
  }
  @keyframes bloom {
    to { transform: translate(var(--dx), -44px) scale(0); opacity: 0; }
  }

  /* Skeleton loaders */
  .skeleton {
    background: linear-gradient(90deg,
      var(--card-bg) 25%, var(--card-hover) 50%, var(--card-bg) 75%);
    background-size: 200% 100%;
    animation: shimmer 1.4s linear infinite;
    border-radius: 8px;
    color: transparent !important;
  }
  .skeleton * { visibility: hidden; }
  @keyframes shimmer { to { background-position: -200% 0; } }

  .pc-top {
    display: flex;
    gap: 14px;
    align-items: center;
  }
  .pc-avatar-wrap {
    position: relative;
    width: 60px;
    height: 60px;
    flex-shrink: 0;
  }
  .pc-avatar {
    width: 52px;
    height: 52px;
    border-radius: 50%;
    background: var(--card-bg);
    border: 2px solid var(--border);
    object-fit: cover;
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
  }
  .orbit-ring {
    position: absolute;
    inset: 0;
    width: 60px; height: 60px;
    animation: orbit 8s linear infinite;
    overflow: visible;
  }
  .orbit-ring circle {
    fill: none;
    stroke: var(--green);
    stroke-width: 2.5;
    stroke-dasharray: 11 7;
    stroke-linecap: round;
    filter: drop-shadow(0 0 5px var(--green));
  }
  @keyframes orbit { to { transform: rotate(360deg); } }
  .player-card[data-status="ingame"] .orbit-ring circle { stroke: var(--blue); filter: drop-shadow(0 0 5px var(--blue)); }
  .player-card[data-status="instudio"] .orbit-ring circle { stroke: var(--yellow); filter: drop-shadow(0 0 5px var(--yellow)); }
  .player-card[data-status="offline"] .orbit-ring { animation: none; opacity: .25; }
  .player-card[data-status="offline"] .orbit-ring circle { stroke: var(--muted); filter: none; }
  .pc-status-ring {
    position: absolute;
    top: -2px;
    left: 50%;
    transform: translateX(-50%);
    width: 8px;
    height: 8px;
    border-radius: 50%;
    border: 1.5px solid var(--panel-bg);
    box-shadow: 0 0 8px currentColor;
    z-index: 2;
  }
  .pc-game-thumb {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    object-fit: cover;
    flex-shrink: 0;
  }

  .pc-identity {
    flex: 1;
    overflow: hidden;
  }
  .pc-display {
    font-size: 15px;
    font-weight: 700;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .pc-user {
    font-size: 12px;
    color: var(--muted);
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 2px;
  }
  .pc-id-badge {
    font-size: 10px;
    background: #111119;
    padding: 1px 6px;
    border-radius: 4px;
    color: var(--muted);
    border: 1px solid var(--border);
    cursor: pointer;
  }
  .pc-id-badge:hover {
    color: var(--text);
    border-color: var(--accent);
  }

  .pc-status-box {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .pc-status-label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    font-weight: 700;
  }
  .pc-status-desc {
    font-size: 11px;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .pc-actions {
    display: flex;
    gap: 8px;
    margin-top: 2px;
  }
  .pc-actions .btn {
    flex: 1;
  }
  .pc-remove-btn {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    width: 32px;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: all 0.2s;
  }
  .pc-remove-btn:hover {
    color: var(--red);
    border-color: var(--red);
    background: rgba(255, 71, 87, 0.1);
  }

  /* Server Finder results */
  .sf-item {
    background: var(--panel-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
  }
  .sf-info {
    display: flex;
    gap: 16px;
    align-items: center;
  }

  /* Quiet in-app status alerts: always available, independent of Windows toasts. */
  #pt-live-alerts {
    position: fixed;
    top: 72px;
    right: 18px;
    z-index: 100000;
    display: flex;
    flex-direction: column;
    gap: 9px;
    width: min(360px, calc(100vw - 28px));
    pointer-events: none;
  }
  .pt-live-alert {
    display: grid;
    grid-template-columns: 34px minmax(0, 1fr) 24px;
    align-items: start;
    gap: 10px;
    padding: 12px 12px 16px 14px;
    background: rgba(24, 24, 38, .97);
    border: 1px solid rgba(46, 204, 113, .38);
    border-left: 3px solid var(--green);
    border-radius: 14px;
    box-shadow: 0 12px 32px rgba(0,0,0,.45);
    backdrop-filter: blur(16px);
    pointer-events: auto;
    position: relative;
    overflow: hidden;
  }
  .pt-live-alert:nth-child(1) { transform: rotate(0deg); z-index: 3; }
  .pt-live-alert:nth-child(2) { transform: rotate(-1.2deg) translateY(3px); z-index: 2; opacity: 0.95; animation-delay: 0.05s; }
  .pt-live-alert:nth-child(3) { transform: rotate(1.2deg) translateY(6px); z-index: 1; opacity: 0.9; animation-delay: 0.1s; }
  .pt-live-alert.slide-right { animation: alertSlideRight .22s ease-out; }
  .pt-live-alert.slide-left  { animation: alertSlideLeft .22s ease-out; }
  @keyframes alertSlideRight { from { opacity: 0; transform: translateX(32px); } to { opacity: 1; transform: translateX(0); } }
  @keyframes alertSlideLeft  { from { opacity: 0; transform: translateX(-32px); } to { opacity: 1; transform: translateX(0); } }
  .pt-live-alert.status-offline { border-color: rgba(141,141,168,.3); border-left-color: var(--muted); }
  .pt-live-alert.status-game { border-left-color: var(--cyan); border-color: rgba(0,210,255,.3); }
  .pt-live-alert-progress {
    position: absolute;
    bottom: 0; left: 0;
    height: 3px;
    background: var(--green);
    border-radius: 0 0 0 14px;
    animation: progress-drain 5.5s linear forwards;
  }
  .status-offline .pt-live-alert-progress { background: var(--muted); }
  .status-game    .pt-live-alert-progress { background: var(--cyan); }
  @keyframes progress-drain { from { width: 100%; } to { width: 0%; } }
  .pt-live-alert-avatar {
    width: 34px; height: 34px; border-radius: 50%; object-fit: cover;
    background: var(--card-bg); border: 1px solid var(--border);
  }
  .pt-live-alert-icon {
    width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center;
    background: var(--green-glow); color: var(--green); font-size: 16px;
  }
  .pt-live-alert-copy { min-width: 0; }
  .pt-live-alert-title { font-size: 12px; font-weight: 750; color: var(--text); line-height: 1.35; }
  .pt-live-alert-message { font-size: 11px; color: var(--muted); margin-top: 3px; line-height: 1.4; overflow-wrap: anywhere; }
  .pt-live-alert-close {
    pointer-events: auto; border: 0; background: transparent; color: var(--muted);
    cursor: pointer; font-size: 16px; padding: 0; line-height: 1;
  }
  .pt-live-alert-close:hover { color: var(--text); }
  @keyframes ptAlertIn { from { opacity: 0; transform: translateY(-7px); } to { opacity: 1; transform: translateY(0); } }
  @media (prefers-reduced-motion: reduce) { .pt-live-alert { animation: none; } }

  /* Server finder extras */
  .sf-density-bar {
    display: flex;
    gap: 2px;
    flex-wrap: wrap;
    align-items: center;
    margin: 4px 0 2px;
  }
  .sf-density-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .sf-density-dot.full  { background: var(--red); }
  .sf-density-dot.half  { background: var(--yellow); }
  .sf-density-dot.empty { background: rgba(141,141,168,.35); }
  .sf-ping-bar-wrap {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--muted);
    margin-top: 2px;
  }
  .sf-ping-bar {
    flex: 1;
    max-width: 80px;
    height: 5px;
    border-radius: 999px;
    background: rgba(255,255,255,.08);
    overflow: hidden;
  }
  .sf-ping-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, var(--green), var(--yellow), var(--red));
  }
  /* Lexis-inspired editorial glass theme: quiet surfaces, bold type, luminous accents. */
  :root {
    --bg: #09090d;
    --panel-bg: rgba(20, 20, 28, .78);
    --card-bg: rgba(27, 27, 38, .72);
    --card-hover: rgba(39, 39, 55, .92);
    --border: rgba(255,255,255,.09);
    --border-bright: rgba(255,255,255,.2);
    --text: #f6f4fb;
    --muted: #9b98aa;
    --accent: #a879ff;
    --accent-hover: #bd9aff;
    --accent-glow: rgba(168,121,255,.28);
    --cyan: #74e8ff;
    --cyan-glow: rgba(116,232,255,.18);
    --green: #61d8a0;
    --green-glow: rgba(97,216,160,.18);
    --red: #ff7180;
    --yellow: #e7c46a;
    --blue: #79a9ff;
  }
  body {
    background:
      radial-gradient(circle at 12% 0%, rgba(168,121,255,.16), transparent 33%),
      radial-gradient(circle at 100% 85%, rgba(116,232,255,.09), transparent 32%),
      var(--bg);
    letter-spacing: -.01em;
  }
  body::before { opacity: .35; background-size: 56px 56px; }
  header {
    background: rgba(9,9,13,.72);
    border-bottom: 1px solid var(--border);
    backdrop-filter: blur(22px) saturate(135%);
    -webkit-backdrop-filter: blur(22px) saturate(135%);
  }
  .brand-title { letter-spacing: -.055em; font-size: 19px; }
  .brand-tag { color: var(--cyan); opacity: .78; letter-spacing: .12em; }
  .tabs { gap: 6px; padding: 5px; border: 1px solid var(--border); border-radius: 14px; background: rgba(255,255,255,.035); }
  .tab { border-radius: 10px; color: #aaa6b8; }
  .tab.active { background: linear-gradient(135deg, rgba(168,121,255,.95), rgba(116,232,255,.72)); color: #100d17; box-shadow: 0 8px 24px var(--accent-glow); }
  .panel {
    background: linear-gradient(145deg, rgba(28,28,39,.86), rgba(13,13,19,.76));
    border: 1px solid var(--border);
    box-shadow: 0 24px 70px rgba(0,0,0,.28);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
  }
  .panel-title { letter-spacing: -.045em; font-size: 26px; }
  .panel-intro { max-width: 720px; color: #aaa6b8; }
  input, select, textarea {
    background: #191923 !important;
    color: #f1eff8 !important;
    border: 1px solid rgba(255,255,255,.14) !important;
    border-radius: 11px;
    caret-color: #b993ff;
  }
  input::placeholder, textarea::placeholder { color: #858292 !important; opacity: 1; }
  input:focus, select:focus, textarea:focus { outline: none; border-color: #b993ff !important; box-shadow: 0 0 0 4px rgba(168,121,255,.13); }
  #pc-left, #pc-right { flex: 1 1 220px; min-width: 0; padding: 12px 14px; }
  #pc-result { grid-template-columns: repeat(2, minmax(0,1fr)); }
  #pc-result .comparison-card { overflow: hidden; }
  #pc-result .comparison-head { display:flex; align-items:center; gap:14px; margin-bottom:16px; }
  #pc-result .comparison-avatar { width:92px;height:92px;object-fit:cover;border-radius:20px;border:1px solid rgba(255,255,255,.14);background:#242431;flex:0 0 auto; }
  #pc-result .comparison-name { font-size:19px;font-weight:800;margin:0 0 4px; }
  #pc-result .comparison-stats { display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:12px; }
  #pc-result .comparison-stat { background:rgba(255,255,255,.035);border:1px solid var(--border);border-radius:10px;padding:10px; }
  #pc-result .comparison-stat label { color:var(--muted);font-size:11px;display:block;margin-bottom:4px; }
  #pc-result .comparison-stat strong { font-size:14px; }
  @media(max-width:720px){#pc-result{grid-template-columns:1fr}#pc-result .comparison-avatar{width:72px;height:72px}}
  .btn { border-radius: 10px; font-weight: 700; letter-spacing: -.01em; }
  .btn:not(.secondary):not(.danger) { background: linear-gradient(135deg, #b993ff, #70dff7); color: #0e0b15; }
  .pf-header, .player-card, .worn-item-card, .profile-signal-card, .sf-item, .tracker-settings-row, .tracker-notification-row {
    background: linear-gradient(145deg, rgba(39,39,54,.68), rgba(18,18,26,.64));
    border-color: rgba(255,255,255,.11);
    box-shadow: inset 0 1px rgba(255,255,255,.045), 0 14px 34px rgba(0,0,0,.18);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
  }
  .player-card:hover, .worn-item-card:hover, .profile-signal-card:hover { border-color: rgba(168,121,255,.62); box-shadow: 0 18px 42px rgba(0,0,0,.3), 0 0 26px rgba(168,121,255,.12); }
  .live-pill { border-color: rgba(116,232,255,.28); background: rgba(116,232,255,.08); }
  .stat-pill { border-radius: 999px; background: rgba(255,255,255,.045); border-color: var(--border); }
  :focus-visible { outline: 2px solid var(--cyan); outline-offset: 3px; }
  .inline-icon { width: 1em; height: 1em; display: inline-block; vertical-align: -.16em; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
  .feature-icon { width: 42px; height: 42px; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round; color: var(--accent); }
  .empty-state-icon .inline-icon { width: 34px; height: 34px; color: var(--accent); }
  .btn .inline-icon { margin-right: .35em; }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: rgba(255,255,255,.025); }
  ::-webkit-scrollbar-thumb { background: linear-gradient(var(--accent), var(--cyan)); border: 3px solid transparent; background-clip: padding-box; border-radius: 999px; }
  .radar-exp-card{display:flex;gap:14px;align-items:center;padding:15px;border:1px solid var(--border);border-radius:14px;background:rgba(255,255,255,.035);margin:10px 0;transition:transform .18s,border-color .18s}.radar-exp-card:hover{transform:translateY(-2px);border-color:rgba(168,121,255,.4)}.radar-exp-card img{width:72px;height:72px;object-fit:cover;border-radius:12px}.radar-exp-card .exp-meta{flex:1;min-width:0}.radar-exp-card .exp-title{font-weight:800;font-size:15px;margin-bottom:6px}.radar-exp-card .exp-stats{display:flex;flex-wrap:wrap;gap:8px;color:var(--muted);font-size:12px}.radar-exp-card .exp-actions{display:flex;gap:6px;flex-wrap:wrap}.radar-exp-card button{padding:6px 10px;font-size:11px}.radar-results{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:14px}.radar-result-card{padding:14px;border:1px solid var(--border);border-radius:14px;background:rgba(255,255,255,.035)}.radar-result-card img{width:48px;height:48px;border-radius:12px;vertical-align:middle;margin-right:10px}.bulk-user-card .bulk-user-avatar{display:block;width:84px;height:84px;border-radius:18px;object-fit:cover;margin:0 0 12px;background:#242431}.bulk-user-card h4{margin:0 0 6px}.radar-result-card h4{display:inline}.radar-result-card .muted{color:var(--muted);font-size:12px}.tab{transition:background .18s,color .18s,transform .18s}.tab:hover{transform:translateY(-1px)}.panel{animation:panel-enter .22s ease both}@keyframes panel-enter{from{opacity:.7;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}
  /* RADAR UI REFINEMENT — layered graphite surfaces, quiet accent, sidebar navigation */
  :root {
    --bg: #101115;
    --panel-bg: #17171d;
    --card-bg: #1d1d25;
    --card-hover: #252530;
    --border: rgba(255,255,255,.075);
    --border-bright: rgba(255,255,255,.13);
    --text: #f1eff6;
    --muted: #96939f;
    --accent: #a18aff;
    --accent-hover: #b7a5ff;
    --accent-glow: rgba(161,138,255,.17);
    --cyan: #7bd8ed;
    --cyan-glow: rgba(123,216,237,.13);
    --green: #61d5a1;
    --green-glow: rgba(97,213,161,.13);
    --red: #f47b89;
    --yellow: #e7bd70;
    --blue: #7daaff;
  }
  body {
    background: radial-gradient(ellipse at 72% 0%, rgba(133,112,209,.08), transparent 42%), var(--bg);
    letter-spacing: -.012em;
  }
  body::before { opacity: .12; background-size: 64px 64px; }
  body::after { animation: none; opacity: .35; background: radial-gradient(circle at 20% 0%, rgba(161,138,255,.10), transparent 35%); }
  .grain-overlay { display: none; }
  header {
    position: fixed; inset: 0 auto 0 0; width: 236px; height: 100vh;
    padding: 22px 14px 16px; display: flex; flex-direction: column; align-items: stretch;
    gap: 24px; background: rgba(19,19,24,.96); border-right: 1px solid var(--border);
    border-bottom: 0; backdrop-filter: blur(18px); z-index: 100;
  }
  .brand-group { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; padding: 3px 8px 17px; border-bottom: 1px solid var(--border); }
  .brand-logo { width: 38px; height: 38px; border-radius: 12px; box-shadow: 0 6px 18px rgba(161,138,255,.18); }
  .brand-title { font-size: 16px; letter-spacing: -.045em; font-weight: 800; }
  .brand-tag { display: none; }
  .live-pill { margin-top: 7px; width: 100%; justify-content: center; font-size: 10px; padding: 7px 9px; background: rgba(97,213,161,.055); border-color: rgba(97,213,161,.16); }
  .tabs { display: flex; flex-direction: column; align-items: stretch; gap: 4px; padding: 0; border: 0; border-radius: 0; background: transparent; }
  .tab { width: 100%; text-align: left; display: flex; align-items: center; padding: 11px 12px; border-radius: 10px; color: #aaa7b3; font-size: 12.5px; font-weight: 650; border: 1px solid transparent; }
  .tab:hover { background: rgba(255,255,255,.035); color: var(--text); transform: none; }
  .tab.active { background: rgba(161,138,255,.105); border-color: rgba(161,138,255,.17); color: #c4b6ff; box-shadow: none; }
  .tab.active::before { display: none; }
  .header-actions { margin-top: auto; padding: 12px 6px 4px; border-top: 1px solid var(--border); flex-wrap: wrap; }
  main { margin-left: 236px; padding: 30px clamp(18px, 3vw, 42px); }
  .panel { max-width: 1280px; }
  .panel-title { font-size: 25px; font-weight: 780; letter-spacing: -.045em; margin-bottom: 10px; }
  .panel-intro { font-size: 13px; line-height: 1.65; color: #aaa7b3; margin-bottom: 22px; }
  .panel { background: transparent; border: 0; box-shadow: none; backdrop-filter: none; -webkit-backdrop-filter: none; }
  .panel.active { animation: panel-enter .18s ease both; }
  .pf-header, .player-card, .worn-item-card, .profile-signal-card, .sf-item, .tracker-settings-row, .tracker-notification-row,
  .radar-exp-card, .radar-result-card, .comparison-card, .bulk-user-card, .panel .card, .panel [class*="card"] {
    background: var(--card-bg); border-color: var(--border); box-shadow: none; backdrop-filter: none; -webkit-backdrop-filter: none;
  }
  .player-card, .sf-item, .pf-header, .comparison-card, .radar-exp-card, .radar-result-card { border-radius: 14px; }
  .player-card:hover, .worn-item-card:hover, .profile-signal-card:hover { border-color: rgba(161,138,255,.32); box-shadow: 0 8px 24px rgba(0,0,0,.16); }
  button, .btn { transition: background .16s ease, border-color .16s ease, color .16s ease, transform .16s ease; border-radius: 9px; }
  .btn:not(.secondary):not(.danger) { background: #a18aff; color: #15121c; box-shadow: none; }
  .btn:not(.secondary):not(.danger):hover { background: #b5a4ff; transform: translateY(-1px); }
  .btn.secondary, .icon-btn { background: #22222a; border: 1px solid var(--border); color: #cbc8d2; }
  input, select, textarea { background: #202027 !important; color: #f2f0f6 !important; border: 1px solid #34343d !important; border-radius: 10px !important; box-shadow: none !important; }
  input::placeholder, textarea::placeholder { color: #888590 !important; opacity: 1; }
  input:focus, select:focus, textarea:focus { border-color: #8d78ed !important; outline: none; box-shadow: 0 0 0 3px rgba(161,138,255,.12) !important; }
  .tabs .tab:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible { outline: 2px solid rgba(161,138,255,.8); outline-offset: 2px; }
  .stat-pill { background: rgba(255,255,255,.035); border-color: var(--border); }
  .live-pill { box-shadow: none; }
  .pt-live-alert { backdrop-filter: blur(12px); }
  ::-webkit-scrollbar-thumb { background: #393743; border: 2px solid transparent; background-clip: padding-box; }
  @media (max-width: 860px) {
    header { width: 190px; padding: 16px 10px; }
    main { margin-left: 190px; padding: 22px 16px; }
    .brand-title { font-size: 14px; }
  }
  @media (max-width: 620px) {
    header { position: sticky; width: 100%; height: auto; inset: auto; padding: 10px 12px; gap: 10px; border-right: 0; border-bottom: 1px solid var(--border); }
    .brand-group { padding: 0 2px; border: 0; flex-wrap: nowrap; }
    .live-pill { margin: 0 0 0 auto; width: auto; }
    .tabs { flex-direction: row; overflow-x: auto; gap: 5px; }
    .tab { width: auto; white-space: nowrap; flex: 0 0 auto; padding: 8px 11px; }
    .header-actions { margin: 0; padding: 4px 0 0; border: 0; justify-content: flex-end; }
    main { margin: 0; padding: 18px 12px; }
  }
  .header-actions {
    flex-wrap: nowrap !important;
    align-items: stretch !important;
    width: 100%;
    gap: 8px;
  }
  @media (max-width: 620px) {
    .header-actions { width: auto !important; flex: 0 0 auto; flex-wrap: nowrap !important; margin-left: auto; padding: 0 !important; gap: 6px; }
  }
  @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; scroll-behavior: auto !important; } }

  /* ========================================================
     ENHANCED TACTICAL RADAR HUD & VISUAL UPGRADES
     ======================================================== */
  /* Tab icons and badge */
  .tab-icon {
    width: 16px;
    height: 16px;
    stroke-width: 2.2px;
    margin-right: 8px;
    flex-shrink: 0;
    opacity: 0.85;
    transition: transform 0.2s ease, opacity 0.2s ease;
  }
  .tab:hover .tab-icon, .tab.active .tab-icon {
    opacity: 1;
    transform: scale(1.1);
  }
  .tab-badge {
    margin-left: auto;
    background: #00e676;
    color: #0b1410;
    font-size: 10px;
    font-weight: 800;
    padding: 2px 7px;
    border-radius: 12px;
    box-shadow: 0 0 10px rgba(0, 230, 118, 0.5);
    letter-spacing: 0.02em;
    animation: badgePulse 2s infinite ease-in-out;
  }
  @keyframes badgePulse {
    0%, 100% { transform: scale(1); box-shadow: 0 0 8px rgba(0, 230, 118, 0.4); }
    50% { transform: scale(1.08); box-shadow: 0 0 16px rgba(0, 230, 118, 0.8); }
  }

  /* CRT Scanlines Overlay Mode */
  body.crt-mode::before {
    content: " ";
    display: block;
    position: fixed;
    top: 0; left: 0; bottom: 0; right: 0;
    background: linear-gradient(rgba(18, 16, 16, 0) 50%, rgba(0, 0, 0, 0.35) 50%), linear-gradient(90deg, rgba(255, 0, 0, 0.03), rgba(0, 255, 0, 0.01), rgba(0, 0, 255, 0.03));
    z-index: 99998;
    background-size: 100% 3px, 6px 100%;
    pointer-events: none;
    opacity: 0.9;
  }
  body.crt-mode::after {
    content: " ";
    display: block;
    position: fixed;
    top: 0; left: 0; bottom: 0; right: 0;
    box-shadow: inset 0 0 80px rgba(0, 0, 0, 0.8);
    z-index: 99997;
    pointer-events: none;
  }

  /* Tactical Radar HUD Component */
  .tactical-radar-hud {
    display: grid;
    grid-template-columns: minmax(280px, 460px) 1fr;
    gap: 20px;
    align-items: center;
    background: radial-gradient(circle at 50% 50%, #151525, #0c0c16);
    border: 1px solid rgba(0, 220, 255, 0.25);
    border-radius: 16px;
    padding: 20px;
    margin-bottom: 24px;
    box-shadow: 0 12px 36px rgba(0, 0, 0, 0.6), inset 0 0 30px rgba(0, 220, 255, 0.04);
    position: relative;
    overflow: hidden;
  }
  .tactical-radar-hud::after {
    content: "";
    position: absolute;
    inset: 0;
    background: radial-gradient(circle at 10% 10%, rgba(161, 138, 255, 0.06), transparent 50%);
    pointer-events: none;
  }
  .radar-scope-container {
    position: relative;
    width: 100%;
    max-width: 460px;
    aspect-ratio: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    box-shadow: 0 0 30px rgba(0, 230, 118, 0.15), inset 0 0 25px rgba(0, 0, 0, 0.9);
  }
  #tracker-radar-canvas {
    width: 100%;
    height: 100%;
    display: block;
    border-radius: 50%;
    cursor: crosshair;
  }
  .radar-hud-overlay {
    position: absolute;
    inset: 10px;
    pointer-events: none;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 10.5px;
    color: rgba(0, 230, 118, 0.75);
    text-shadow: 0 0 6px rgba(0, 230, 118, 0.6);
  }
  .radar-hud-tl { position: absolute; top: 12px; left: 14px; }
  .radar-hud-tr { position: absolute; top: 12px; right: 14px; text-align: right; }
  .radar-hud-bl { position: absolute; bottom: 12px; left: 14px; }
  .radar-hud-br { position: absolute; bottom: 12px; right: 14px; text-align: right; }

  .radar-legend-card {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 10px 14px;
  }
  .radar-legend-title {
    font-size: 16px;
    font-weight: 700;
    color: #e2dcff;
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
  }
  .radar-legend-item {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13px;
    color: #cac5d8;
  }
  .legend-blip {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .legend-blip.ingame { background: #00e676; box-shadow: 0 0 10px #00e676; }
  .legend-blip.instudio { background: #ffb300; box-shadow: 0 0 10px #ffb300; }
  .legend-blip.online { background: #00d2ff; box-shadow: 0 0 10px #00d2ff; }
  .legend-blip.offline { background: #5c596b; }
  .radar-quick-hint {
    margin-top: 10px;
    padding: 10px 14px;
    background: rgba(161, 138, 255, 0.1);
    border: 1px solid rgba(161, 138, 255, 0.2);
    border-radius: 8px;
    font-size: 12px;
    color: #d1c6ff;
    line-height: 1.5;
  }

  @media (max-width: 900px) {
    .tactical-radar-hud {
      grid-template-columns: 1fr;
      justify-items: center;
    }
  }

  /* Enhanced Glowing Status Halo & Depth */
  .player-card {
    position: relative;
    border-radius: 16px;
    transition: transform 0.22s cubic-bezier(0.2, 0.8, 0.2, 1), box-shadow 0.22s, border-color 0.22s !important;
    perspective: 1000px;
  }
  .player-card[data-status="ingame"] {
    border-color: rgba(0, 230, 118, 0.45) !important;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4), 0 0 16px rgba(0, 230, 118, 0.12) !important;
  }
  .player-card[data-status="ingame"]:hover {
    border-color: #00e676 !important;
    box-shadow: 0 16px 36px rgba(0, 0, 0, 0.5), 0 0 28px rgba(0, 230, 118, 0.38) !important;
  }
  .player-card[data-status="instudio"] {
    border-color: rgba(255, 179, 0, 0.4) !important;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4), 0 0 16px rgba(255, 179, 0, 0.12) !important;
  }
  .player-card[data-status="instudio"]:hover {
    border-color: #ffb300 !important;
    box-shadow: 0 16px 36px rgba(0, 0, 0, 0.5), 0 0 26px rgba(255, 179, 0, 0.35) !important;
  }
  .player-card[data-status="online"] {
    border-color: rgba(0, 210, 255, 0.4) !important;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4), 0 0 16px rgba(0, 210, 255, 0.12) !important;
  }
  .player-card[data-status="online"]:hover {
    border-color: #00d2ff !important;
    box-shadow: 0 16px 36px rgba(0, 0, 0, 0.5), 0 0 26px rgba(0, 210, 255, 0.35) !important;
  }

  /* Shimmer / Skeleton Animation for Live Updates */
  .skeleton-shimmer {
    background: linear-gradient(90deg, rgba(255, 255, 255, 0.03) 25%, rgba(255, 255, 255, 0.08) 50%, rgba(255, 255, 255, 0.03) 75%);
    background-size: 200% 100%;
    animation: shimmerSlide 1.5s infinite;
  }
  @keyframes shimmerSlide {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }


  /* Interactive Button Click Feedback & Ripple Animations */
  button, .btn {
    position: relative;
    overflow: hidden;
    transform: translateZ(0);
    transition: transform 0.12s cubic-bezier(0.2, 0, 0, 1), background 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease !important;
  }
  button:active, .btn:active {
    transform: scale(0.96) translateY(1px) !important;
    filter: brightness(0.92);
  }
  button.btn-loading, .btn.btn-loading {
    pointer-events: none;
    opacity: 0.85;
  }
  .btn-loader-spinner {
    display: inline-block;
    width: 13px;
    height: 13px;
    border: 2px solid rgba(255, 255, 255, 0.3);
    border-radius: 50%;
    border-top-color: #ffffff;
    animation: btnSpin 0.7s infinite linear;
    vertical-align: middle;
    margin-right: 6px;
  }
  @keyframes btnSpin {
    to { transform: rotate(360deg); }
  }

  /* Catalog Explorer Enhanced Styling */
  #cx-results {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
    gap: 16px;
    margin-top: 18px;
  }
  .catalog-card {
    background: var(--card-bg) !important;
    border: 1px solid var(--border) !important;
    border-radius: 14px !important;
    padding: 12px !important;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
  }
  .catalog-card:hover {
    transform: translateY(-3px);
    border-color: rgba(161, 138, 255, 0.5) !important;
    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.35);
  }
  .catalog-thumb-wrap {
    position: relative;
    width: 100%;
    aspect-ratio: 1;
    background: #14141e;
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    margin-bottom: 10px;
  }
  .catalog-thumb {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .catalog-thumb-placeholder {
    font-size: 36px;
  }
  .catalog-favs {
    position: absolute;
    bottom: 6px;
    right: 6px;
    background: rgba(0, 0, 0, 0.75);
    backdrop-filter: blur(4px);
    font-size: 10.5px;
    font-weight: 700;
    color: #ffca28;
    padding: 2px 6px;
    border-radius: 6px;
  }
  .catalog-title {
    font-size: 13.5px;
    font-weight: 700;
    color: #f1eff6;
    margin: 0 0 4px;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    line-height: 1.35;
  }
  .catalog-creator {
    font-size: 11.5px;
    color: var(--muted);
    margin-bottom: 8px;
  }
  .catalog-creator b {
    color: #b7a5ff;
  }
  .catalog-price-row {
    margin-bottom: 10px;
  }
  .catalog-price {
    font-size: 13px;
    font-weight: 800;
    color: #00e676;
    text-shadow: 0 0 8px rgba(0, 230, 118, 0.3);
  }
  .catalog-free-badge {
    background: #00e676;
    color: #0c1812;
    font-size: 10px;
    font-weight: 800;
    padding: 2px 7px;
    border-radius: 6px;
  }
  .catalog-actions .btn {
    width: 100%;
    text-align: center;
    display: block;
  }
  .group-hero-card {
    background: linear-gradient(135deg, rgba(29, 29, 37, 0.95), rgba(20, 20, 28, 0.95)) !important;
    border-color: rgba(161, 138, 255, 0.3) !important;
    padding: 20px !important;
  }
  .group-hero-avatar {
    width: 90px !important;
    height: 90px !important;
    border-radius: 18px !important;
    border: 2px solid rgba(161, 138, 255, 0.4) !important;
  }

</style>
</head>
<body>
<div class="grain-overlay" aria-hidden="true"></div>
<div id="pt-live-alerts" aria-live="polite" aria-relevant="additions" aria-label="Tracked player status updates"></div>

<!-- SPLASH SCREEN OVERLAY -->
<div id="splash-screen" class="splash-overlay">
  <div class="splash-card">
    <div class="splash-radar-wrap">
      <div class="splash-radar-outer"></div>
      <div class="splash-radar">
        <div class="splash-radar-sweep"></div>
        <div class="splash-radar-rings"></div>
        <div class="splash-radar-crosshair-h"></div>
        <div class="splash-radar-crosshair-v"></div>
        <div class="splash-blip blip-1" style="top: 28%; left: 72%;"></div>
        <div class="splash-blip blip-2" style="top: 68%; left: 32%;"></div>
        <div class="splash-blip blip-3" style="top: 42%; left: 22%;"></div>
        <div class="splash-radar-center">
          <span class="splash-radar-icon">R</span>
        </div>
      </div>
    </div>
    <div class="splash-title">ROBLOX RADAR</div>
    <div class="splash-tagline">GLOBALSTATS TELEMETRY SUITE</div>
    <div class="splash-bar-wrap">
      <div id="splash-bar" class="splash-bar-fill"></div>
    </div>
    <div id="splash-status" class="splash-status">Initializing telemetry & radar sweep...</div>
  </div>
</div>

<header>
  <div class="brand-group">
    <div class="brand-logo">R</div>
    <div class="brand-title">Roblox Radar</div>
    <div class="brand-tag">GlobalStats</div>
    <div class="live-pill"><span class="live-dot"></span> LIVE &nbsp;·&nbsp; 0 online / 0 tracked</div>
  </div>
  <div class="tabs">
    <button class="tab" data-tab="server"><svg class="inline-icon tab-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.5 3.5 5.5 3.5 9S14.5 18.5 12 21c-2.5-2.5-3.5-5.5-3.5-9S9.5 5.5 12 3Z"/></svg> <span>Servers</span></button>
    <button class="tab active" data-tab="player"><svg class="inline-icon tab-icon" viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.5"/><path d="M4.5 21a7.5 7.5 0 0 1 15 0"/></svg> <span>Profile</span></button>
    <button class="tab" data-tab="tracker"><svg class="inline-icon tab-icon" viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 9 9M12 7a5 5 0 1 0 5 5M12 12l7-7"/><circle cx="12" cy="12" r="1"/></svg> <span>Tracker</span> <span class="tab-badge" id="tracker-online-badge" style="display:none;">0</span></button>
    <button class="tab" data-tab="gameradar"><svg class="inline-icon tab-icon" viewBox="0 0 24 24"><path d="M7 9h10a4 4 0 0 1 3.8 5.2l-1.2 4a2 2 0 0 1-3.6.5L14 16H10l-2 2.7a2 2 0 0 1-3.6-.5l-1.2-4A4 4 0 0 1 7 9Z"/><path d="M7 12v4M5 14h4M16 13h.01M19 15h.01"/></svg> <span>Game Radar</span></button>
    <button class="tab" data-tab="explorer"><svg class="inline-icon tab-icon" viewBox="0 0 24 24"><circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 5 5"/></svg> <span>Explorer</span></button>
    <button class="tab" data-tab="groups"><svg class="inline-icon tab-icon" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0M16 5a3 3 0 0 1 0 6M17 14a5 5 0 0 1 4 6"/></svg> <span>Groups</span></button>
    <button class="tab" data-tab="catalog"><svg class="inline-icon tab-icon" viewBox="0 0 24 24"><path d="m8 4 4 2 4-2 5 4-3 4-2-2v10H8V10l-2 2-3-4 5-4Z"/></svg> <span>Catalog</span></button>
  </div>
  <div class="header-actions">
    <button class="icon-btn" id="btn-toggle-crt" title="Toggle Tactical Scanlines HUD"><svg class="inline-icon" viewBox="0 0 24 24"><rect width="20" height="15" x="2" y="3" rx="2"/><path d="m7 21 5-3 5 3M9 8h6M9 12h6"/></svg></button>
    <button class="icon-btn" id="btn-toggle-sfx" title="Toggle Sci-Fi Audio Telemetry"><svg class="inline-icon" id="sfx-icon" viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg></button>
    <button class="icon-btn" id="btn-replay-splash" title="Replay Splash Screen"><svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m13 2-8 12h6l-1 8 8-12h-6l1-8Z"/></svg></button>
  </div>
</header>

<main>
  <!-- SERVER FINDER PANEL -->
  <section class="panel" id="panel-server">
    <div class="panel-title">Server Discovery</div>
    <div class="form-group">
      <label>PLACE ID OR GAME LINK:</label>
      <input type="text" id="sf-place-id" placeholder="Enter Place ID (e.g. 2753915549)" />
    </div>
    <div class="row" style="margin-bottom: 12px;">
      <button class="btn secondary" id="sf-get-tab">Paste Clipboard</button>
      <button class="btn" id="sf-find">Find Servers</button>
    </div>
    <div class="form-group row" style="align-items: center;">
      <label style="margin:0; flex:0 0 auto;">MAX SERVERS:</label>
      <input type="number" id="sf-max" value="20" style="width: 70px; flex:0 0 auto;" />
    </div>
    <div class="form-group" style="display:flex; align-items:center; gap:8px;">
      <input type="checkbox" id="sf-hide-full" checked style="width:auto; flex:0 0 auto;" />
      <label for="sf-hide-full" style="margin:0; text-transform:none; font-size:12px;">Hide Full Servers</label>
    </div>
    <div id="sf-status" class="error-msg"></div>
    <div id="sf-results" style="margin-top: 16px;"></div>
  </section>

  <!-- PLAYER PROFILE PANEL -->
  <section class="panel active" id="panel-player">
    <div class="panel-title">Player Profile</div>
    <div class="panel-intro">A clearer snapshot of the public Roblox profile: account status, account age, username history, presence, and current avatar items. No Roblox login is needed.</div>
    <div class="form-group">
      <label>ROBLOX USERNAME OR USERID:</label>
      <div class="row">
        <input type="text" id="pf-input" placeholder="e.g. Wolfpaq or 60596019" />
        <button class="btn secondary" id="pf-paste" type="button">Paste</button>
        <button class="btn" id="pf-find">Search Profile</button>
      </div>
    </div>
    <div id="pf-error" class="error-msg" style="margin-bottom: 16px;"></div>

    <div id="pf-result-container" style="display:none;">
      <!-- Profile Header -->
      <div class="pf-header">
        <div class="pf-avatar-wrap">
          <img id="pf-avatar-full" class="pf-avatar-full" src="" alt="Avatar" />
          <div class="pf-avatar-scanline"></div>
        </div>
        <div class="pf-details">
          <div class="pf-name">
            <span id="pf-display-name"></span>
            <span id="pf-verified" class="verified-badge" style="display:none;" title="Verified Account">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 2l3.09 3.26L19.5 6l1.24 4.24L22 14l-2.26 3.65L18.5 22l-4.24-1.24L10 22l-1.26-3.74L4.5 17 3.26 12.76 2 8.5l3.26-3.09L6.5 2l4.24 1.24z"></path>
                <path d="m9 12 2 2 4-4"></path>
              </svg>
            </span>
          </div>
          <div class="pf-username" id="pf-username"></div>
          <div class="pf-status-row">
            <span class="status-chip unavailable" id="pf-account-status">Public status unavailable</span>
            <span class="fav-meta">Current public flag only</span>
          </div>
          <div class="pf-actions">
            <a id="pf-roblox-link" class="btn secondary small" target="_blank" rel="noopener noreferrer">Open Roblox Profile</a>
            <button id="pf-copy-id" class="btn secondary small">Copy User ID</button>
            <button id="pf-track-btn" class="btn small" style="background:var(--cyan); color:#000;">+ Track Player</button>
          </div>
          <div class="pf-bio" id="pf-bio"></div>
          <div class="pf-stats">
            <span>Friends: <strong id="pf-count-friends">Unavailable</strong></span>
            <span>Followers: <strong id="pf-count-followers">Unavailable</strong></span>
            <span>Following: <strong id="pf-count-following">Unavailable</strong></span>
            <span>Joined: <strong id="pf-created">Unknown</strong></span>
            <span>Presence: <strong id="pf-status-text">Unavailable</strong></span>
          </div>
        </div>
      </div>

      <div class="profile-signal-grid">
        <div class="profile-signal-card">
          <div class="profile-signal-label">Account age</div>
          <div class="profile-signal-value" id="pf-signal-age">Unavailable</div>
          <div class="profile-signal-detail">Calculated from the public creation date.</div>
        </div>
        <div class="profile-signal-card">
          <div class="profile-signal-label">Username history</div>
          <div class="profile-signal-value" id="pf-signal-history">Unavailable</div>
          <div class="profile-signal-detail">Previous usernames returned by Roblox.</div>
        </div>
        <div class="profile-signal-card">
          <div class="profile-signal-label">Verified badge</div>
          <div class="profile-signal-value" id="pf-signal-verified">Unavailable</div>
          <div class="profile-signal-detail">Only the public verified-badge field.</div>
        </div>
        <div class="profile-signal-card">
          <div class="profile-signal-label">Public description</div>
          <div class="profile-signal-value" id="pf-signal-description">Unavailable</div>
          <div class="profile-signal-detail">Whether a description was returned.</div>
        </div>
      </div>
      <div class="profile-note">Public signals are context, not a trust score or safety guarantee. “Unavailable” means Roblox did not return that field; it does not mean zero, false, or suspicious.</div>

      <!-- Sub-tabs -->
      <div class="sub-tabs">
        <button class="sub-tab active" data-subtab="overview">Overview</button>
        <button class="sub-tab" data-subtab="history">Username History</button>
        <button class="sub-tab" data-subtab="favorites">Favorite Games</button>
        <button class="sub-tab" data-subtab="friends">Friends</button>
        <button class="sub-tab" data-subtab="groups">Groups</button>
        <button class="sub-tab" data-subtab="badges">Badges</button>
      </div>

      <!-- Overview: Currently Worn Items -->
      <div class="sub-panel active" id="pfc-overview">
        <label>CURRENTLY WORN ASSETS</label>
        <div id="pf-worn-list" class="grid-cards" style="margin-top: 10px;"></div>
      </div>

      <div class="sub-panel" id="pfc-history">
        <label>PREVIOUS USERNAMES</label>
        <div id="pf-history-note" class="fav-meta" style="margin-top:8px;"></div>
        <ul id="pf-history-list" class="username-history-list"></ul>
        <div class="history-actions">
          <button id="pf-history-more" class="btn secondary small" style="display:none;">Load older names</button>
          <span id="pf-history-count" class="fav-meta"></span>
        </div>
      </div>

      <!-- Favorites -->
      <div class="sub-panel" id="pfc-favorites">
        <label>FAVORITE EXPERIENCES</label>
        <div id="pf-favs-list" class="grid-cards" style="margin-top: 10px;"></div>
      </div>

      <!-- Friends (Under Construction) -->
      <div class="sub-panel" id="pfc-friends">
        <div class="construction-card">
          <div class="construction-icon"><svg class="feature-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20 12 4l8 16M7 15h10M9 11h6M5 20h14"/></svg></div>
          <div class="construction-title">Friends List Under Construction</div>
          <div class="construction-desc">
            Direct friend list inspection is temporarily paused while we work on official data integration without bypassing Roblox security.<br><br>
            <strong>Note:</strong> Total friend count is verified and displayed in the profile header above.
          </div>
          <div class="construction-badge">Security Compliant</div>
        </div>
      </div>

      <!-- Groups -->
      <div class="sub-panel" id="pfc-groups">
        <label>PLAYER GROUPS</label>
        <div id="pf-groups-list" class="grid-cards" style="margin-top: 10px;"></div>
      </div>

      <!-- Badges -->
      <div class="sub-panel" id="pfc-badges">
        <div class="construction-banner">
          <span class="construction-badge"><svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20 12 4l8 16M7 15h10M9 11h6"/></svg> Under Construction</span>
          <span>Game badges are temporarily paused until a stable, unblocked API method is supported without bypassing Roblox security. Official profile badges are displayed below.</span>
        </div>
        <label>OFFICIAL ROBLOX PROFILE BADGES</label>
        <div id="pf-badges-list" class="grid-badges" style="margin-top: 10px;"></div>
      </div>
    </div>
  </section>

  <!-- NEW DISCOVERY PANELS -->
  <section class="panel" id="panel-gameradar">
    <div class="panel-title">Game Radar</div><div class="panel-intro">Inspect public experience statistics and save games to a watchlist. Historical player counts are recorded locally while Radar is running.</div>
    <div class="form-group"><label>PLACE ID, UNIVERSE ID, OR EXPERIENCE LINK</label><div class="row"><input id="gr-input" placeholder="Experience URL or Place ID"><button class="btn" id="gr-search">Analyze</button><button class="btn secondary" id="gr-add">Add to Watchlist</button></div></div>
    <div id="gr-result"></div><h3 style="margin-top:22px">Tracked Experiences</h3><button class="btn secondary small" id="gr-refresh">Refresh Watchlist</button><div id="gr-watchlist" class="grid-cards" style="margin-top:12px"></div>
  </section>
  <section class="panel" id="panel-explorer">
    <div class="panel-title">User Explorer</div><div class="panel-intro">Bulk resolve usernames and user IDs (up to 100 inputs). Add results to the player tracker or open profiles.</div>
    <div class="form-group"><label>USERNAMES OR USER IDs (one per line or comma-separated)</label><textarea id="ue-input" rows="5" placeholder="Builderman&#10;Roblox&#10;1234567"></textarea></div><button class="btn" id="ue-search">Resolve Users</button><div id="ue-results" class="grid-cards" style="margin-top:14px"></div><h3 style="margin-top:24px">Profile Comparison</h3><div class="row"><input id="pc-left" placeholder="Username / ID one"><input id="pc-right" placeholder="Username / ID two"><button class="btn" id="pc-run">Compare</button></div><div id="pc-result" class="radar-results"></div>
  </section>
  <section class="panel" id="panel-groups">
    <div class="panel-title">Group Explorer</div><div class="panel-intro">Inspect public group information, role structure, and experiences owned by a group.</div>
    <div class="form-group"><label>GROUP ID OR GROUP LINK</label><div class="row"><input id="gx-input" placeholder="Group ID"><button class="btn" id="gx-search">Explore Group</button></div></div><div id="gx-result"></div>
  </section>
  <section class="panel" id="panel-catalog">
    <div class="panel-title">Catalog Explorer</div><div class="panel-intro">Search public catalog listings. Prices and availability may change and are shown only when Roblox returns them.</div>
    <div class="form-group"><label>ITEM SEARCH</label><div class="row"><input id="cx-input" placeholder="e.g. black headphones"><button class="btn" id="cx-search">Search Catalog</button></div></div><div id="cx-results" class="grid-cards"></div>
  </section>

  <!-- ENHANCED PLAYER TRACKER PANEL -->
  <section class="panel" id="panel-tracker">
    <div class="tracker-header">
      <div class="panel-title" style="margin-bottom:0;">Player Tracker</div>
      <div class="tracker-controls">
        <button class="btn secondary small" id="pt-refresh-btn"><svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14-4L4 9M4 5v4h4M4 13a8 8 0 0 0 14 4l2-2M20 19v-4h-4"/></svg> Refresh All</button>
        <button class="btn danger small" id="pt-clear">Clear All</button>
      </div>
    </div>
    <div class="panel-intro">Follow public presence changes for the players you add. Pause or remove a player at any time; checks stop when Roblox Radar is closed.</div>

    <!-- Shown only when winotify was installed during this launch. This stays
         in normal document flow and never covers tracker controls. -->
    <div id="pt-notification-restart-banner" class="notification-restart-banner" role="status" aria-live="polite">
      <div class="notification-restart-copy">
        <div class="notification-restart-title">Windows notifications need one restart</div>
        <div class="notification-restart-description">Roblox Radar just installed the Windows toast notification package. Relaunch the app to load it and enable toast alerts.</div>
      </div>
      <button class="btn small" id="pt-relaunch-notifications"><svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14-4L4 9M4 5v4h4M4 13a8 8 0 0 0 14 4l2-2M20 19v-4h-4"/></svg> Relaunch Roblox Radar</button>
    </div>

    <!-- Quick Add Form -->
    <div class="form-group">
      <label>ADD PLAYER (USERNAME OR USER ID):</label>
      <div class="row">
        <input type="text" id="pt-input" placeholder="e.g. Wolfpaq or 60596019" />
        <button class="btn secondary" id="pt-paste" type="button">Paste</button>
        <button class="btn" id="pt-add">+ Add to Radar</button>
      </div>
      <div id="pt-error" class="error-msg" style="margin-top: 6px;"></div>
    </div>

    <div class="tracker-settings-row">
      <label for="pt-interval" style="margin:0; text-transform:none; letter-spacing:0;">Refresh interval:</label>
      <select id="pt-interval" aria-label="Presence refresh interval">
        <option value="30">Every 30 seconds</option>
        <option value="45">Every 45 seconds</option>
        <option value="60" selected>Every 1 minute</option>
        <option value="90">Every 90 seconds</option>
        <option value="120">Every 2 minutes</option>
        <option value="300">Every 5 minutes</option>
      </select>
      <span id="pt-refresh-note" class="fav-meta">Checks run while Roblox Radar is open; paused players are not checked.</span>
    </div>
    <div class="tracker-notification-row">
      <label class="tracker-notification-toggle" for="pt-notifications">
        <input type="checkbox" id="pt-notifications" />
        Windows toast alerts
      </label>
      <button class="btn secondary small" id="pt-test-notification">Test notification</button>
      <span id="pt-notification-note" class="notification-note">Off by default. Alerts appear only while Roblox Radar is running.</span>
    </div>
    <div class="tracker-notification-row">
      <label class="tracker-notification-toggle" for="pt-notification-sound">
        <input type="checkbox" id="pt-notification-sound" />
        Play a sound for status changes
      </label>
      <span class="notification-note">Enables the Windows toast sound and a short local in-app tone.</span>
    </div>

    <!-- Status Summary Bar -->
    <div class="tracker-summary-bar">
      <div class="stat-pill"><span class="dot total"></span> Total: <span class="count" id="pt-stat-total">0</span></div>
      <div class="stat-pill"><span class="dot online"></span> Online: <span class="count" id="pt-stat-online">0</span></div>
      <div class="stat-pill"><span class="dot ingame"></span> In Game: <span class="count" id="pt-stat-ingame">0</span></div>
      <div class="stat-pill"><span class="dot instudio"></span> Studio: <span class="count" id="pt-stat-instudio">0</span></div>
      <div class="stat-pill"><span class="dot offline"></span> Offline: <span class="count" id="pt-stat-offline">0</span></div>
    </div>

    <!-- Filter input when multiple players tracked -->
    <div class="form-group" id="pt-filter-group" style="display:none;">
      <input type="text" id="pt-filter-input" placeholder="Filter tracked players by name..." style="padding: 6px 12px; font-size: 12px;" />
    </div>

    <!-- Real-time Tactical Radar Scope Canvas -->
    <div class="tactical-radar-hud">
      <div class="radar-scope-container">
        <canvas id="tracker-radar-canvas" width="460" height="460"></canvas>
        <div class="radar-hud-overlay">
          <div class="radar-hud-tl">SYS: TACTICAL TELEMETRY</div>
          <div class="radar-hud-tr" id="radar-hud-pings">SWEEP: ACTIVE · 0 TARGETS</div>
          <div class="radar-hud-bl">FREQ: 9.4 GHz · RANGE: LIVE</div>
          <div class="radar-hud-br">GLOBALSTATS SCANNER</div>
        </div>
      </div>
      <div class="radar-legend-card">
        <div class="radar-legend-title"><svg class="inline-icon" viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 9 9M12 7a5 5 0 1 0 5 5M12 12l7-7"/><circle cx="12" cy="12" r="1"/></svg> Live Radar Scope Legend</div>
        <div class="radar-legend-item"><span class="legend-blip ingame"></span> <b>In Game:</b> Green pulse blip (click to track)</div>
        <div class="radar-legend-item"><span class="legend-blip instudio"></span> <b>In Studio:</b> Amber pulse blip</div>
        <div class="radar-legend-item"><span class="legend-blip online"></span> <b>Website / App:</b> Cyan pulse blip</div>
        <div class="radar-legend-item"><span class="legend-blip offline"></span> <b>Offline:</b> Slate outer perimeter blip</div>
        <div class="radar-quick-hint">💡 Hover or click blips on the radar scope to immediately scroll & focus on that player's dossier below!</div>
      </div>
    </div>

    <!-- Tracker List / Grid -->
    <div id="pt-list" class="tracker-grid"></div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
const ready = () => new Promise((res) => window.pywebview?.api ? res() : window.addEventListener("pywebviewready", res, {once:true}));
const ICON = {
  globe: '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.5 3.5 5.5 3.5 9S14.5 18.5 12 21c-2.5-2.5-3.5-5.5-3.5-9S9.5 5.5 12 3Z"/></svg>',
  user: '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M4.5 21a7.5 7.5 0 0 1 15 0"/></svg>',
  game: '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 9h10a4 4 0 0 1 3.8 5.2l-1.2 4a2 2 0 0 1-3.6.5L14 16H10l-2 2.7a2 2 0 0 1-3.6-.5l-1.2-4A4 4 0 0 1 7 9Z"/><path d="M7 12v4M5 14h4M16 13h.01M19 15h.01"/></svg>',
  radar: '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a9 9 0 1 0 9 9M12 7a5 5 0 1 0 5 5M12 12l7-7"/><circle cx="12" cy="12" r="1"/></svg>',
  tag: '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 5v6l10 10 8-8L11 3H5a2 2 0 0 0-2 2Z"/><circle cx="8" cy="8" r="1"/></svg>',
  shirt: '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m8 4 4 2 4-2 5 4-3 4-2-2v10H8V10l-2 2-3-4 5-4Z"/></svg>',
  star: '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"/></svg>',
  users: '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0M16 5a3 3 0 0 1 0 6M17 14a5 5 0 0 1 4 6"/></svg>',
  medal: '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m8 3 4 6 4-6M6 13a6 6 0 1 0 12 0 6 6 0 0 0-12 0Z"/><path d="m9 18-1 4 4-2 4 2-1-4"/></svg>'
};

function escapeHtml(str) {
  if (!str) return "";
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function formatCount(value) {
  if (value === null || value === undefined || value === "") return "Unavailable";
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString() : "Unavailable";
}

// ---------------------------------------------------------
// Smooth Fluid Splash Loader with Smooth Counter
// ---------------------------------------------------------
async function runSplash() {
  const splash = $("splash-screen");
  const bar = $("splash-bar");
  const status = $("splash-status");
  if (!splash) return;

  splash.classList.remove("hidden");

  const steps = [
    { target: 20,  msg: "Connecting to Roblox presence service…" },
    { target: 45,  msg: "Loading local JSON caches & radar data…" },
    { target: 75,  msg: "Syncing cached player status…" },
    { target: 92,  msg: "Calibrating telemetry sweep…" },
    { target: 100, msg: "Radar systems online. Ready!" }
  ];

  let currentPct = 0;

  try {
    for (const step of steps) {
      if (status) status.textContent = step.msg;

      // Smooth counter progression
      while (currentPct < step.target) {
        currentPct += 1;
        if (bar) bar.style.width = currentPct + "%";
        await new Promise(r => setTimeout(r, 12));
      }
      await new Promise(r => setTimeout(r, 220));
    }
    await new Promise(r => setTimeout(r, 280));
  } catch (err) {
    console.error("runSplash error:", err);
  } finally {
    splash.classList.add("hidden");
  }
}

$("btn-replay-splash").addEventListener("click", () => runSplash());

// Trigger splash as soon as script evaluates
runSplash();

// ---------------------------------------------------------
// Navigation Tabs
// ---------------------------------------------------------
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("panel-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "tracker") refreshTracker();
  });
});


// ---------------------------------------------------------
// PLAYER PROFILE
// ---------------------------------------------------------
let currentPlayer = null;
let loadedTabs = { favs: false, friends: false, groups: false, badges: false };

document.querySelectorAll(".sub-tab").forEach((btn) => {
  btn.addEventListener("click", async () => {
    document.querySelectorAll(".sub-tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".sub-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    const target = btn.dataset.subtab;
    $("pfc-" + target).classList.add("active");

    if (!currentPlayer) return;
    const uid = currentPlayer.user.id;
    if (target === "favorites" && !loadedTabs.favs) loadedTabs.favs = await loadFavorites(uid);
    else if (target === "groups" && !loadedTabs.groups) loadedTabs.groups = await loadGroups(uid);
    else if (target === "badges" && !loadedTabs.badges) loadedTabs.badges = await loadBadges(uid);
  });
});

function renderUsernameHistory(history) {
  const names = history && Array.isArray(history.names) ? history.names : [];
  const list = $("pf-history-list");
  const note = $("pf-history-note");
  const moreButton = $("pf-history-more");
  const count = $("pf-history-count");

  if (!history || !history.available) {
    note.textContent = (history && history.message) ||
      "Username history is unavailable. This does not mean the account has never changed names.";
    list.innerHTML = "";
    list.className = "username-history-list";
    count.textContent = "";
    moreButton.style.display = "none";
    return;
  }
  if (!names.length) {
    note.textContent = "Roblox returned no previous usernames for this account.";
    list.innerHTML = `<div class="empty-state" style="grid-column:1/-1;">
      <div class="empty-state-icon">${ICON.tag}</div>
      <div class="empty-state-title">No username changes</div>
      <div class="empty-state-desc">This player has always used their current username.</div>
    </div>`;
    list.className = "username-history-list";
    count.textContent = "0 previous usernames returned";
  } else {
    note.textContent = "Names are shown oldest first. These are usernames, not display-name history.";
    list.className = "timeline-container";
    list.innerHTML = names.map((name, idx) =>
      `<div class="timeline-item">
        <span class="timeline-name">@${escapeHtml(name)}</span>
        <span class="timeline-tag">${idx === 0 ? "Initial Username" : `Alias #${idx}`}</span>
      </div>`
    ).join("");
    count.textContent = `Showing ${names.length} previous ${names.length === 1 ? "username" : "usernames"}`;
  }
  moreButton.style.display = history.nextCursor ? "inline-flex" : "none";
}

async function loadMoreUsernameHistory() {
  if (!currentPlayer || !currentPlayer.username_history?.nextCursor) return;
  const button = $("pf-history-more");
  const oldText = button.textContent;
  button.disabled = true;
  button.textContent = "Loading...";
  try {
    const page = await window.pywebview.api.get_username_history(
      currentPlayer.user.id,
      currentPlayer.username_history.nextCursor
    );
    if (!page.available) {
      $("pf-history-note").textContent = page.message ||
        "More username history could not be loaded. The names already shown are unchanged.";
      return;
    }
    currentPlayer.username_history.names = [
      ...currentPlayer.username_history.names,
      ...page.names
    ];
    currentPlayer.username_history.nextCursor = page.nextCursor;
    renderUsernameHistory(currentPlayer.username_history);
    const historySignal = $("pf-signal-history");
    if (historySignal) historySignal.textContent = `${currentPlayer.username_history.names.length} returned`;
  } catch (error) {
    $("pf-history-note").textContent = "Could not load more history: " + error.message;
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
}

$("pf-history-more").addEventListener("click", loadMoreUsernameHistory);

async function searchProfile(query) {
  if (!query) return;
  $("pf-error").textContent = "";
  $("pf-result-container").style.display = "none";
  $("pf-find").disabled = true;
  $("pf-find").textContent = "Loading...";
  
  currentPlayer = null;
  loadedTabs = { favs: false, friends: false, groups: false, badges: false };
  document.querySelectorAll(".sub-tab").forEach((b) => b.classList.remove("active"));
  document.querySelectorAll(".sub-panel").forEach((p) => p.classList.remove("active"));
  document.querySelector('.sub-tab[data-subtab="overview"]').classList.add("active");
  $("pfc-overview").classList.add("active");

  try {
    const res = await window.pywebview.api.get_full_profile(query);
    if (res.error) {
      $("pf-error").textContent = res.error;
    } else {
      currentPlayer = res;
      $("pf-result-container").style.display = "block";
      
      $("pf-avatar-full").src = res.avatar_full || "";
      $("pf-display-name").textContent = res.user.displayName;
      $("pf-username").textContent = "@" + res.user.name;
      $("pf-roblox-link").href = `https://www.roblox.com/users/${res.user.id}/profile`;
      const accountStatus = (res.account_status && res.account_status.label) || "Unavailable";
      const statusChip = $("pf-account-status");
      statusChip.textContent = `Public status: ${accountStatus}`;
      statusChip.className = "status-chip " + (
        accountStatus === "Active" ? "active" :
        accountStatus === "Banned / Terminated" ? "terminated" : "unavailable"
      );
      $("pf-copy-id").onclick = async () => {
        try {
          await navigator.clipboard.writeText(String(res.user.id));
          $("pf-copy-id").textContent = "Copied!";
          setTimeout(() => $("pf-copy-id").textContent = "Copy User ID", 1200);
        } catch {
          $("pf-copy-id").textContent = `ID: ${res.user.id}`;
        }
      };

      $("pf-track-btn").onclick = async () => {
        const button = $("pf-track-btn");
        const oldText = button.textContent;
        button.disabled = true;
        button.textContent = "Adding…";
        try {
          const r = await window.pywebview.api.add_tracked(String(res.user.id));
          if (r && r.error) {
            $("pf-error").textContent = r.error;
          } else {
            button.innerHTML = '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg> Tracked!';
            setTimeout(() => { button.textContent = oldText; button.disabled = false; }, 1500);
          }
        } catch (error) {
          $("pf-error").textContent = "Could not add player to tracker: " + (error.message || error);
        } finally {
          if (button.textContent === "Adding…") { button.textContent = oldText; button.disabled = false; }
        }
      };

      $("pf-bio").textContent = res.bio || "No bio provided.";
      $("pf-verified").style.display = res.verified ? "inline-flex" : "none";
      const createdYears = res.account_age_years ? `${res.account_age_years} years ago` : res.created;
      $("pf-created").innerHTML = `<span title="${escapeHtml(res.created || '')}">${escapeHtml(createdYears)}</span>`;
      if (typeof res.counts?.friends === "number") {
        $("pf-count-friends").textContent = "0";
        countUp($("pf-count-friends"), res.counts.friends);
      } else {
        $("pf-count-friends").textContent = formatCount(res.counts.friends);
      }
      if (typeof res.counts?.followers === "number") {
        $("pf-count-followers").textContent = "0";
        countUp($("pf-count-followers"), res.counts.followers);
      } else {
        $("pf-count-followers").textContent = formatCount(res.counts.followers);
      }
      if (typeof res.counts?.following === "number") {
        $("pf-count-following").textContent = "0";
        countUp($("pf-count-following"), res.counts.following);
      } else {
        $("pf-count-following").textContent = formatCount(res.counts.following);
      }
      const pfHdr = document.querySelector(".pf-header");
      if (pfHdr) {
        pfHdr.onmousemove = e => {
          const r = pfHdr.getBoundingClientRect();
          pfHdr.style.setProperty("--mx", (e.clientX - r.left) + "px");
          pfHdr.style.setProperty("--my", (e.clientY - r.top) + "px");
        };
      }
      $("pf-signal-age").textContent = Number.isFinite(res.account_age_years)
        ? `${res.account_age_years} years (approx.)`
        : "Unavailable";
      const history = res.username_history || {available: false, names: [], nextCursor: null};
      $("pf-signal-history").textContent = history.available
        ? `${history.names.length} returned`
        : "Unavailable";
      $("pf-signal-verified").textContent = res.profile_available
        ? (res.verified ? "Badge returned" : "No badge returned")
        : "Unavailable";
      $("pf-signal-description").textContent = res.profile_available
        ? (res.bio && res.bio.trim() ? "Description present" : "No description returned")
        : "Unavailable";
      renderUsernameHistory(history);
      
      // Online Status Logic
      let st = res.presence_available ? "Offline / unavailable" : "Unavailable";
      let color = "var(--muted)";
      const p = res.presence || {};
      if (p.userPresenceType === 2) {
        st = p.lastLocation || "In Game";
        color = "var(--blue)";
      } else if (p.userPresenceType === 3) {
        st = "In Studio";
        color = "var(--yellow)";
      } else if (p.userPresenceType === 1) {
        st = "Online";
        color = "var(--green)";
      }
      $("pf-status-text").textContent = st;
      $("pf-status-text").style.color = color;

      // Render Worn
      const wList = $("pf-worn-list");
      if (!res.worn_items.length) {
        wList.innerHTML = `<div class="empty-state" style="grid-column:1/-1;">
          <div class="empty-state-icon">${ICON.shirt}</div>
          <div class="empty-state-title">No worn items</div>
          <div class="empty-state-desc">${res.inventory_api_key_required ? "Roblox returned 401 Unauthorized. You will need to enter your own Roblox Open Cloud API key before this inventory endpoint can be used." : (res.worn_available ? "No current outfit items were returned by Roblox." : "Current outfit data is unavailable.")}</div>
        </div>`;
      } else {
        wList.innerHTML = res.worn_items.map(i => {
          const itemUrl = `https://www.roblox.com/catalog/${encodeURIComponent(i.id)}`;
          const priceText = i.price === null || i.price === undefined
            ? "Price unavailable"
            : `${formatCount(i.price)} Robux`;
          return `
          <div class="worn-item-card">
            <div class="worn-item-thumb-wrap">
              <img src="${i.thumbnail}" class="worn-item-thumb" alt="${escapeHtml(i.name)}" onerror="this.style.visibility='hidden'">
              <div class="worn-price-pill">${escapeHtml(priceText)}</div>
              <div class="worn-scrim">
                <div class="worn-title" title="${escapeHtml(i.name)}">${escapeHtml(i.name)}</div>
                <a class="btn secondary small" style="margin-top:6px;" href="${itemUrl}" target="_blank" rel="noopener noreferrer">View Item</a>
              </div>
            </div>
          </div>`;
        }).join("");
      }
    }
  } catch (e) {
    $("pf-error").textContent = "Profile lookup error: " + e.message;
  } finally {
    $("pf-find").disabled = false;
    $("pf-find").textContent = "Search Profile";
  }
}

$("pf-input").addEventListener("keydown", (e) => { if (e.key === "Enter") searchProfile($("pf-input").value.trim()); });
$("pf-find").addEventListener("click", () => searchProfile($("pf-input").value.trim()));

function renderSkeletons(count = 4) {
  let html = "";
  for (let i = 0; i < count; i++) {
    html += '<div class="fav-card skeleton" style="height:140px; pointer-events:none;"></div>';
  }
  return html;
}

async function loadFavorites(uid) {
  const c = $("pf-favs-list");
  c.innerHTML = renderSkeletons(6);
  const res = await window.pywebview.api.get_user_favorites(uid);
  if (res.error) { c.innerHTML = `<div class="error-msg">${escapeHtml(res.error)}</div>`; return false; }
  if (!res.favorites.length) {
    c.innerHTML = `<div class="empty-state" style="grid-column:1/-1;">
          <div class="empty-state-icon">${ICON.star}</div>
      <div class="empty-state-title">No favorite experiences</div>
      <div class="empty-state-desc">This player has not favorited any public experiences yet.</div>
    </div>`;
    return true;
  }
  c.innerHTML = res.favorites.map(f => {
    const known = f.playing !== null && f.playing !== undefined && Number.isFinite(Number(f.playing));
    const count = known ? Number(f.playing) : null;
    const zero = count === 0;
    const live = known ? `<div class="game-playing"><span class="game-live-dot ${zero ? "zero" : "active"}"></span><span>${count.toLocaleString()} ${count === 1 ? "person" : "people"} playing now</span></div>` : '<div class="fav-meta">Live player count unavailable</div>';
    return `
    <a class="fav-card" href="${f.url}" target="_blank" rel="noopener noreferrer" style="text-decoration:none; color:inherit;">
      <img src="${f.thumbnail}" alt="${escapeHtml(f.name)}" onerror="this.style.visibility='hidden'">
      <div class="fav-info">
        <div class="fav-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</div>
        <div class="fav-meta">By ${escapeHtml(f.creator)}</div>
        ${live}
      </div>
    </a>`;
  }).join("");
  return true;
}

async function loadGroups(uid) {
  const c = $("pf-groups-list");
  c.innerHTML = renderSkeletons(4);
  const res = await window.pywebview.api.get_user_groups(uid);
  if (res.error) { c.innerHTML = `<div class="error-msg">${escapeHtml(res.error)}</div>`; return false; }
  if (!res.groups.length) {
    c.innerHTML = `<div class="empty-state" style="grid-column:1/-1;">
          <div class="empty-state-icon">${ICON.users}</div>
      <div class="empty-state-title">No public groups</div>
      <div class="empty-state-desc">This account is not a public member of any Roblox groups.</div>
    </div>`;
    return true;
  }
  c.innerHTML = res.groups.map(g => `
    <a class="fav-card" href="${g.url}" target="_blank" rel="noopener noreferrer" style="text-decoration:none; color:inherit;">
      <img src="${g.icon}" alt="${escapeHtml(g.name)}" onerror="this.style.visibility='hidden'">
      <div class="fav-info">
        <div class="fav-name" title="${escapeHtml(g.name)}">${escapeHtml(g.name)}</div>
        <div class="fav-meta">${escapeHtml(g.role)} · ${g.memberCount.toLocaleString()} members</div>
      </div>
    </a>
  `).join("");
  return true;
}

async function loadBadges(uid) {
  const c = $("pf-badges-list");
  c.innerHTML = renderSkeletons(6);
  const res = await window.pywebview.api.get_user_badges(uid);
  if (res.error) { c.innerHTML = `<div class="error-msg">${escapeHtml(res.error)}</div>`; return false; }
  if (!res.badges.length) {
    c.innerHTML = `<div class="empty-state" style="grid-column:1/-1;">
          <div class="empty-state-icon">${ICON.medal}</div>
      <div class="empty-state-title">No profile badges</div>
      <div class="empty-state-desc">No official profile badges were returned for this account.</div>
    </div>`;
    return true;
  }
  c.innerHTML = res.badges.map(b => `
    <div class="fav-card">
      ${b.icon ? `<img src="${b.icon}" alt="${escapeHtml(b.name)}" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'badge-image-fallback',textContent:'Badge icon unavailable'}))">` : `<div class="badge-image-fallback">Badge icon unavailable</div>`}
      <div class="fav-info">
        <div class="fav-name" title="${escapeHtml(b.name)}">${escapeHtml(b.name)}</div>
        <div class="fav-meta">${escapeHtml(b.type)}</div>
        ${b.description ? `<div style="font-size:11px; color:var(--muted); margin-top:4px; line-height:1.3;">${escapeHtml(b.description)}</div>` : ""}
      </div>
    </div>
  `).join("");
  return true;
}

// ---------------------------------------------------------
// ENHANCED PLAYER TRACKER
// ---------------------------------------------------------
let currentTrackerData = [];
let trackerPollTimer = null;
let lastRefreshRequestedAt = 0;
let refreshCooldownUntil = 0;
let windowsNotificationsEnabled = false;
let notificationSoundEnabled = false;

function formatFreshness(iso) {
  if (!iso) return "No successful presence refresh yet";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Last update time unavailable";
  return "Last updated " + d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

function showInAppPresenceAlerts(transitions) {
  if (!Array.isArray(transitions) || !transitions.length) return;
  const host = $("pt-live-alerts");
  if (!host) return;
  // Keep the UI calm if several tracked players change at once.
  const changes = transitions.slice(0, 3);
  for (const change of changes) {
    const name = change.displayName || change.name || "A tracked player";
    const status = String(change.to || "status changed");
    const isOnline = status === "Online" || status === "In Game" || status === "In Studio";
    const isGame = status === "In Game" || status === "In Studio";
    const slideClass = isOnline ? "slide-right" : "slide-left";
    const card = document.createElement("div");
    card.className = "pt-live-alert " + slideClass + (!isOnline ? " status-offline" : (isGame ? " status-game" : ""));
    card.setAttribute("role", "status");
    const avatar = currentTrackerData.find(p => String(p.id) === String(change.userId))?.avatar;
    const avatarNode = avatar
      ? `<img class="pt-live-alert-avatar" src="${escapeHtml(avatar)}" alt="" />`
      : `<div class="pt-live-alert-icon" aria-hidden="true">${isOnline ? "●" : "○"}</div>`;
    card.innerHTML = `${avatarNode}
      <div class="pt-live-alert-copy">
        <div class="pt-live-alert-title">${escapeHtml(name)} is now ${escapeHtml(status)}</div>
        <div class="pt-live-alert-message">${isOnline ? "A tracked player just came online." : "Tracked player presence changed."}</div>
      </div>
      <button class="pt-live-alert-close" type="button" aria-label="Dismiss notification">×</button>
      <div class="pt-live-alert-progress"></div>`;
    const dismiss = () => {
      if (card.parentNode) {
        card.style.opacity = "0";
        card.style.transform = "translateY(-5px)";
        setTimeout(() => card.remove(), 180);
      }
    };
    card.querySelector(".pt-live-alert-close").addEventListener("click", dismiss);
    host.prepend(card);
    // Maximum three visible alerts; older ones quietly disappear.
    while (host.children.length > 3) host.lastElementChild.remove();
    window.setTimeout(dismiss, 5500);
  }
  if (transitions.length > changes.length) {
    const summary = document.createElement("div");
    summary.className = "pt-live-alert slide-right";
    summary.innerHTML = `<div class="pt-live-alert-icon" aria-hidden="true">${ICON.radar}</div>
      <div class="pt-live-alert-copy"><div class="pt-live-alert-title">${transitions.length - changes.length} more status updates</div>
      <div class="pt-live-alert-message">Open Player Tracker to see the latest statuses.</div></div>
      <button class="pt-live-alert-close" type="button" aria-label="Dismiss notification">×</button>
      <div class="pt-live-alert-progress"></div>`;
    summary.querySelector("button").addEventListener("click", () => summary.remove());
    host.prepend(summary);
    while (host.children.length > 3) host.lastElementChild.remove();
    window.setTimeout(() => summary.remove(), 5500);
  }
}

async function sendPresenceNotifications(transitions) {
  // In-app alerts are always shown, even when Windows toast notifications are disabled.
  showInAppPresenceAlerts(transitions);
  if (notificationSoundEnabled && Array.isArray(transitions) && transitions.length) playNotificationSound();
  if (!windowsNotificationsEnabled || !Array.isArray(transitions) || !transitions.length) return;
  for (const change of transitions.slice(0, 5)) {
    try {
      const name = change.displayName || change.name || "A tracked player";
      const result = await window.pywebview.api.send_windows_notification(
        "Roblox Radar · Status changed",
        `${name} is now ${change.to}.`,
        null,
        change.avatar || ""
      );
      if (result.error) {
        $("pt-notification-note").textContent = result.error;
        return;
      }
    } catch (error) {
      $("pt-notification-note").textContent = "Could not show Windows notification: " + error.message;
      return;
    }
  }
  if (transitions.length > 5) {
    $("pt-notification-note").textContent = `Showing up to 5 alerts per refresh; ${transitions.length - 5} additional changes were not notified.`;
  }
}

function playNotificationSound() {
  try {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return;
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = 660;
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.08, ctx.currentTime + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.18);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.2);
    osc.addEventListener("ended", () => ctx.close());
  } catch (_) {}
}

async function refreshTracker(manual = false) {
  if (!window.pywebview?.api) return;
  const now = Date.now();
  if (manual && now < refreshCooldownUntil) {
    const remain = Math.ceil((refreshCooldownUntil - now) / 1000);
    $("pt-refresh-note").textContent = `Please wait ${remain}s before refreshing again; cached status remains visible.`;
    return;
  }
  const refreshBtn = $("pt-refresh-btn");
  if (refreshBtn) refreshBtn.textContent = "⏳ Updating...";
  try {
    const result = await window.pywebview.api.get_presence(false);
    const list = Array.isArray(result) ? result : (result.players || []);
    currentTrackerData = list;
    const configuredInterval = Number(result.refreshIntervalSeconds || $("pt-interval").value || 60);
    $("pt-interval").value = String(configuredInterval);
    const cooldown = Number(result.nextRefreshIn || 0);
    if (cooldown > 0) refreshCooldownUntil = Date.now() + cooldown * 1000;
    else refreshCooldownUntil = Date.now() + Math.min(configuredInterval, 60) * 1000;
    const note = result.refreshError || `Presence synced. Auto-refresh every ${configuredInterval}s while Roblox Radar is open.`;
    $("pt-refresh-note").textContent = note;
    renderTrackerList();
    if (result.transitions && result.transitions.length) {
      for (const change of result.transitions) {
        const card = document.querySelector(`.player-card[data-uid="${change.userId}"]`);
        if (card) {
          const toStatus = String(change.to || "").toLowerCase();
          if (toStatus.includes("game")) {
            card.classList.add("flash-ingame");
            bloom(card, "var(--blue)");
            setTimeout(() => card.classList.remove("flash-ingame"), 700);
          } else if (toStatus.includes("online")) {
            card.classList.add("flash-online");
            bloom(card, "var(--green)");
            setTimeout(() => card.classList.remove("flash-online"), 700);
          } else {
            card.classList.add("flash-offline");
            setTimeout(() => card.classList.remove("flash-offline"), 700);
          }
        }
      }
    }
    await sendPresenceNotifications(result.transitions || []);
  } catch (e) {
    $("pt-refresh-note").textContent = "Could not refresh presence: " + e.message;
  } finally {
    if (refreshBtn) refreshBtn.innerHTML = ICON.radar + " Refresh All";
  }
}

function renderTrackerList() {
  const c = $("pt-list");
  const filterVal = ($("pt-filter-input").value || "").toLowerCase().trim();
  if (!currentTrackerData || currentTrackerData.length === 0) {
    c.innerHTML = `<div class="empty-state" style="grid-column:1/-1;">
      <div class="empty-state-icon" aria-hidden="true">${ICON.radar}</div>
      <div class="empty-state-title">Nothing on radar yet</div>
      <div class="empty-state-desc">Add a Roblox username or User ID above to start tracking presence in real time.</div>
      <button class="btn" onclick="$('pt-input').focus()">+ Add your first player</button>
    </div>`;
    updateTrackerStats(0, 0, 0, 0, 0);
    $("pt-filter-group").style.display = "none";
    return;
  }
  $("pt-filter-group").style.display = currentTrackerData.length > 2 ? "block" : "none";
  let onlineCount = 0, inGameCount = 0, studioCount = 0, offlineCount = 0;
  const filtered = currentTrackerData.filter(u => {
    const pType = (u.presence || {}).userPresenceType || 0;
    if (!u.paused) {
      if (pType === 2) inGameCount++;
      else if (pType === 3) studioCount++;
      else if (pType === 1) onlineCount++;
      else offlineCount++;
    }
    return !filterVal || (u.displayName || "").toLowerCase().includes(filterVal) ||
      (u.name || "").toLowerCase().includes(filterVal) || String(u.id).includes(filterVal);
  });
  updateTrackerStats(currentTrackerData.length, onlineCount, inGameCount, studioCount, offlineCount);
  if (!filtered.length) {
    c.innerHTML = `<div class="filter-no-results">
      <div class="crosshair-icon"><div class="crosshair-ring"></div></div>
      <div class="filter-no-results-title">No players match "${escapeHtml(filterVal)}"</div>
      <div class="filter-no-results-desc">Try a different name, username, or ID.</div>
    </div>`;
    return;
  }
  c.innerHTML = filtered.map(u => {
    const p = u.presence || {};
    const pType = p.userPresenceType || 0;
    let statusLabel = "Offline / unavailable", statusClass = "offline", statusRingColor = "var(--muted)", desc = "Offline, or presence is limited to friends", joinBtn = "", gamePill = "";
    if (u.paused) { statusLabel = "Paused"; statusClass = "offline"; statusRingColor = "var(--yellow)"; desc = "Presence checks paused for this player"; }
    else if (pType === 2) {
      statusLabel = "In Game"; statusClass = "ingame"; statusRingColor = "var(--blue)";
      const gameName = p.lastLocation || "an experience";
      desc = `Playing: ${escapeHtml(gameName)}`;
      if (p.placeId) joinBtn = `<button class="btn small" style="background:var(--blue);" onclick="joinGame(${p.placeId}, '${p.gameId || ""}')">${ICON.game} Join Game</button>`;
      const thumbIcon = p.placeId
        ? `<img class="pc-game-thumb" src="https://assetgame.roblox.com/Game/Tools/ThumbnailAsset.ashx?aid=${p.placeId}&fmt=png&wd=48&ht=48" alt="" onerror="this.style.display='none'">`
        : ICON.game;
      gamePill = `<div class="pc-game-pill">${thumbIcon}<span>In Game · ${escapeHtml(gameName)}</span></div>`;
    } else if (pType === 3) { statusLabel = "In Studio"; statusClass = "instudio"; statusRingColor = "var(--yellow)"; desc = "Editing inside Roblox Studio"; }
    else if (pType === 1) { statusLabel = "Online"; statusClass = "online"; statusRingColor = "var(--green)"; desc = "Active on website or app"; }
    else if (p.lastOnline) {
      const d = new Date(p.lastOnline);
      if (!Number.isNaN(d.getTime())) desc = "Last seen: " + d.toLocaleString();
    }
    const updated = u.updatedAt ? formatFreshness(u.updatedAt) : "No successful presence refresh yet";
    const stale = u.stale || !u.updatedAt;
    const staleText = stale ? ` · ${u.paused ? "Paused" : "Cached / stale"}` : " · Live";
    return `
      <div class="player-card" data-status="${statusClass}" data-uid="${u.id}">
        <div class="pc-top">
          <div class="pc-avatar-wrap">
            <svg class="orbit-ring" viewBox="0 0 100 100" aria-hidden="true">
              <circle cx="50" cy="50" r="46" />
            </svg>
            <img src="${u.avatar || ""}" class="pc-avatar" alt="${escapeHtml(u.displayName)}" onerror="this.style.visibility='hidden'">
            <div class="pc-status-ring dot ${statusClass}" style="background:${statusRingColor}"></div>
          </div>
          <div class="pc-identity">
            <div class="pc-display" title="${escapeHtml(u.displayName)}">${escapeHtml(u.displayName)} ${u.paused ? '<span class="pc-paused-badge">PAUSED</span>' : ''}</div>
            <div class="pc-user"><span>@${escapeHtml(u.name)}</span><span class="pc-id-badge" onclick="copyText('${u.id}', this)">ID: ${u.id}</span></div>
            ${gamePill}
          </div>
        </div>
        <div class="pc-status-box">
          <div class="pc-status-label" style="color:${statusRingColor};"><span class="dot ${statusClass}"></span> ${statusLabel}</div>
          <div class="pc-status-desc" title="${escapeHtml(desc)}">${escapeHtml(desc)}</div>
          <div class="pc-freshness ${stale ? "stale" : ""}">${escapeHtml(updated + staleText)}</div>
        </div>
        <div class="pc-actions">
          ${joinBtn}
          <button class="btn secondary small" onclick="inspectPlayer('${u.id}')">${ICON.user} Profile</button>
          <button class="btn secondary small pc-pause-btn" onclick="togglePlayerPause(${u.id}, ${!u.paused})">${u.paused ? "▶ Resume" : "⏸ Pause"}</button>
          <button class="pc-remove-btn" onclick="removePlayer(${u.id})" title="Remove from Tracker"><svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
        </div>
      </div>`;
  }).join("");
}


// ---------------------------------------------------------
// Tactical Audio SFX & CRT Telemetry Engine
// ---------------------------------------------------------
let sfxEnabled = true;
function playTacticalPing(type = "sweep") {
  if (!sfxEnabled) return;
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    if (type === "ingame") {
      // High-tech lock-on chirp
      osc.type = "sine";
      osc.frequency.setValueAtTime(880, ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(1760, ctx.currentTime + 0.12);
      gain.gain.setValueAtTime(0.001, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.08, ctx.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.18);
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.2);
    } else if (type === "click") {
      // Soft mechanical tactile click
      osc.type = "triangle";
      osc.frequency.setValueAtTime(440, ctx.currentTime);
      gain.gain.setValueAtTime(0.03, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.04);
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.05);
    } else {
      // Soft sonar sweep ping
      osc.type = "sine";
      osc.frequency.setValueAtTime(540, ctx.currentTime);
      gain.gain.setValueAtTime(0.001, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.04, ctx.currentTime + 0.03);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.25);
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.26);
    }
    osc.addEventListener("ended", () => ctx.close());
  } catch (_) {}
}

// CRT Scanlines Toggle
const btnToggleCrt = $("btn-toggle-crt");
if (btnToggleCrt) {
  btnToggleCrt.addEventListener("click", () => {
    document.body.classList.toggle("crt-mode");
    playTacticalPing("click");
  });
}

// Audio SFX Toggle
const btnToggleSfx = $("btn-toggle-sfx");
if (btnToggleSfx) {
  btnToggleSfx.addEventListener("click", () => {
    sfxEnabled = !sfxEnabled;
    btnToggleSfx.style.opacity = sfxEnabled ? "1" : "0.4";
    if (sfxEnabled) playTacticalPing("ingame");
  });
}

// Play click sound on tab navigation
document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => playTacticalPing("click"));
});

// ---------------------------------------------------------
// Tactical Radar Scope Canvas Renderer (Sweep & Blips)
// ---------------------------------------------------------
class TacticalRadarScope {
  constructor(canvasId) {
    this.canvas = $(canvasId);
    if (!this.canvas) return;
    this.ctx = this.canvas.getContext("2d");
    this.sweepAngle = 0;
    this.blips = [];
    this.animId = null;
    this.initEvents();
    this.startLoop();
  }

  updateTargets(players) {
    if (!players || !players.length) {
      this.blips = [];
      const pingsEl = $("radar-hud-pings");
      if (pingsEl) pingsEl.textContent = "SWEEP: ACTIVE · 0 TARGETS";
      return;
    }

    const pingsEl = $("radar-hud-pings");
    if (pingsEl) pingsEl.textContent = `SWEEP: ACTIVE · ${players.length} TARGETS`;

    // Map each player to a polar position (angle + distance)
    this.blips = players.map((p, idx) => {
      const pType = (p.presence || {}).userPresenceType || 0;
      let color = "#5c596b", radiusRatio = 0.85; // offline
      if (p.paused) {
        color = "#ffb300"; radiusRatio = 0.75;
      } else if (pType === 2) {
        color = "#00e676"; radiusRatio = 0.35 + (idx % 3) * 0.12; // In Game closer to center
      } else if (pType === 3) {
        color = "#ffb300"; radiusRatio = 0.55; // Studio
      } else if (pType === 1) {
        color = "#00d2ff"; radiusRatio = 0.68; // Online
      }

      // Distribute evenly in circle with deterministic hash offset
      const angle = (idx / players.length) * (Math.PI * 2) + 0.3;
      return {
        id: p.id,
        name: p.displayName || p.name,
        angle: angle,
        distRatio: radiusRatio,
        color: color,
        pType: pType,
        pingPulse: 0
      };
    });
  }

  startLoop() {
    const render = () => {
      this.draw();
      this.sweepAngle = (this.sweepAngle + 0.035) % (Math.PI * 2);
      this.animId = requestAnimationFrame(render);
    };
    this.animId = requestAnimationFrame(render);
  }

  draw() {
    if (!this.ctx) return;
    const ctx = this.ctx;
    const w = this.canvas.width;
    const h = this.canvas.height;
    const cx = w / 2;
    const cy = h / 2;
    const maxR = cx - 18;

    // Background fade for phosphor sweep tail
    ctx.clearRect(0, 0, w, h);

    // Radar Dark Base
    const bgGrad = ctx.createRadialGradient(cx, cy, 10, cx, cy, maxR);
    bgGrad.addColorStop(0, "#081014");
    bgGrad.addColorStop(1, "#030608");
    ctx.fillStyle = bgGrad;
    ctx.beginPath();
    ctx.arc(cx, cy, maxR, 0, Math.PI * 2);
    ctx.fill();

    // Concentric Range Rings
    ctx.strokeStyle = "rgba(0, 230, 118, 0.22)";
    ctx.lineWidth = 1.2;
    for (let r = maxR * 0.25; r <= maxR; r += maxR * 0.25) {
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.stroke();
    }

    // Crosshairs
    ctx.strokeStyle = "rgba(0, 230, 118, 0.28)";
    ctx.beginPath();
    ctx.moveTo(cx - maxR, cy); ctx.lineTo(cx + maxR, cy);
    ctx.moveTo(cx, cy - maxR); ctx.lineTo(cx, cy + maxR);
    ctx.stroke();

    // Diagonal tick marks
    ctx.strokeStyle = "rgba(0, 230, 118, 0.12)";
    ctx.beginPath();
    const dR = maxR * 0.707;
    ctx.moveTo(cx - dR, cy - dR); ctx.lineTo(cx + dR, cy + dR);
    ctx.moveTo(cx - dR, cy + dR); ctx.lineTo(cx + dR, cy - dR);
    ctx.stroke();

    // Rotating Sweep Cone
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, maxR, this.sweepAngle - 0.45, this.sweepAngle);
    ctx.closePath();
    const sweepGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, maxR);
    sweepGrad.addColorStop(0, "rgba(0, 230, 118, 0.45)");
    sweepGrad.addColorStop(1, "rgba(0, 230, 118, 0.0)");
    ctx.fillStyle = sweepGrad;
    ctx.fill();

    // Sweep Leading Line
    ctx.strokeStyle = "#00e676";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(this.sweepAngle) * maxR, cy + Math.sin(this.sweepAngle) * maxR);
    ctx.stroke();
    ctx.restore();

    // Draw Targets (Blips)
    for (const b of this.blips) {
      const bx = cx + Math.cos(b.angle) * (b.distRatio * maxR);
      const by = cy + Math.sin(b.angle) * (b.distRatio * maxR);

      // Check if sweep line just passed this blip
      const angleDiff = (this.sweepAngle - b.angle + Math.PI * 4) % (Math.PI * 2);
      if (angleDiff < 0.08) {
        b.pingPulse = 1.0;
      }

      // Outer ripple ring on sweep pulse
      if (b.pingPulse > 0.02) {
        ctx.strokeStyle = b.color;
        ctx.globalAlpha = b.pingPulse;
        ctx.beginPath();
        ctx.arc(bx, by, 7 + (1 - b.pingPulse) * 16, 0, Math.PI * 2);
        ctx.stroke();
        b.pingPulse *= 0.94; // Decay pulse
      }

      // Solid blip dot
      ctx.globalAlpha = 1;
      ctx.fillStyle = b.color;
      ctx.shadowColor = b.color;
      ctx.shadowBlur = 10;
      ctx.beginPath();
      ctx.arc(bx, by, 4.5, 0, Math.PI * 2);
      ctx.fill();

      // Blip callsign text
      ctx.shadowBlur = 0;
      ctx.fillStyle = "#ffffff";
      ctx.font = "9px Consolas, monospace";
      ctx.fillText(b.name.substring(0, 10), bx + 8, by + 3);
    }

    // Outer Bezel Ring
    ctx.strokeStyle = "rgba(0, 230, 118, 0.55)";
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(cx, cy, maxR + 1, 0, Math.PI * 2);
    ctx.stroke();
  }

  initEvents() {
    this.canvas.addEventListener("click", (e) => {
      const rect = this.canvas.getBoundingClientRect();
      const scaleX = this.canvas.width / rect.width;
      const scaleY = this.canvas.height / rect.height;
      const mx = (e.clientX - rect.left) * scaleX;
      const my = (e.clientY - rect.top) * scaleY;
      const cx = this.canvas.width / 2;
      const cy = this.canvas.height / 2;
      const maxR = cx - 18;

      // Check which blip was clicked
      for (const b of this.blips) {
        const bx = cx + Math.cos(b.angle) * (b.distRatio * maxR);
        const by = cy + Math.sin(b.angle) * (b.distRatio * maxR);
        const dist = Math.hypot(mx - bx, my - by);
        if (dist <= 18) {
          playTacticalPing("ingame");
          const targetCard = document.querySelector(`.player-card[data-uid="${b.id}"]`);
          if (targetCard) {
            targetCard.scrollIntoView({ behavior: "smooth", block: "center" });
            targetCard.style.outline = "2px solid #00e676";
            setTimeout(() => targetCard.style.outline = "none", 1200);
          }
          break;
        }
      }
    });
  }
}

let tacticalScopeInstance = null;

function updateTrackerStats(total, online, ingame, studio, offline) {
  countUp($("pt-stat-total"), total);
  countUp($("pt-stat-online"), online);
  countUp($("pt-stat-ingame"), ingame);
  countUp($("pt-stat-instudio"), studio);
  countUp($("pt-stat-offline"), offline);

  // Update Tracker tab live count badge
  const badgeEl = $("tracker-online-badge");
  const liveOnline = online + ingame + studio;
  if (badgeEl) {
    if (liveOnline > 0) {
      badgeEl.textContent = liveOnline;
      badgeEl.style.display = "inline-block";
    } else {
      badgeEl.style.display = "none";
    }
  }

  // Update header LIVE pill
  const livePillText = document.querySelector(".live-pill");
  if (livePillText) {
    livePillText.innerHTML = `<span class="live-dot"></span> LIVE &nbsp;·&nbsp; ${liveOnline} online / ${total} tracked`;
  }

  // Update tactical radar scope target blips
  if (!tacticalScopeInstance) {
    tacticalScopeInstance = new TacticalRadarScope("tracker-radar-canvas");
  }
  if (tacticalScopeInstance) {
    tacticalScopeInstance.updateTargets(currentTrackerData || []);
  }
}

// ---------------------------------------------------------
// Count-up animation for stat numbers
// ---------------------------------------------------------
function countUp(el, to) {
  if (!el) return;
  const from = parseInt(el.textContent, 10) || 0;
  if (from === to) return;
  const dur = 300, t0 = performance.now();
  (function tick(now) {
    const k = Math.min(1, (now - t0) / dur);
    const ease = 1 - Math.pow(1 - k, 3);
    el.textContent = Math.round(from + (to - from) * ease);
    if (k < 1) requestAnimationFrame(tick);
  })(t0);
}

// ---------------------------------------------------------
// 3D tilt + cursor spotlight on player cards
// ---------------------------------------------------------
function attachCardMousemove() {
  const list = $("pt-list");
  if (!list || list.dataset.mouseDelegated) return;
  list.dataset.mouseDelegated = "true";
  list.addEventListener("mousemove", e => {
    const card = e.target.closest(".player-card");
    if (!card || !list.contains(card)) return;
    const r = card.getBoundingClientRect();
    card.style.setProperty("--mx", (e.clientX - r.left) + "px");
    card.style.setProperty("--my", (e.clientY - r.top) + "px");
  });
}
attachCardMousemove();
// Wire spotlight on pf-header (profile header)
document.addEventListener("DOMContentLoaded", () => {
  const pfHeader = document.querySelector(".pf-header");
  if (pfHeader) {
    pfHeader.addEventListener("mousemove", e => {
      const r = pfHeader.getBoundingClientRect();
      pfHeader.style.setProperty("--mx", (e.clientX - r.left) + "px");
      pfHeader.style.setProperty("--my", (e.clientY - r.top) + "px");
    });
  }
});

// ---------------------------------------------------------
// Global presence bloom particles
// ---------------------------------------------------------
function bloom(card, color = "var(--green)") {
  if (!card) return;
  for (let i = 0; i < 6; i++) {
    const p = document.createElement("div");
    p.className = "bloom-particle";
    p.style.backgroundColor = color;
    p.style.boxShadow = `0 0 6px ${color}`;
    p.style.left = "50%";
    p.style.top = "50%";
    p.style.setProperty("--dx", (Math.random() * 60 - 30) + "px");
    card.appendChild(p);
    setTimeout(() => p.remove(), 900);
  }
}

// ---------------------------------------------------------
// Tactile button ripple
// ---------------------------------------------------------
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".btn");
  if (!btn) return;
  const rect = btn.getBoundingClientRect();
  const circle = document.createElement("span");
  const d = Math.max(rect.width, rect.height);
  circle.style.width = circle.style.height = `${d}px`;
  circle.style.left = `${e.clientX - rect.left - d / 2}px`;
  circle.style.top = `${e.clientY - rect.top - d / 2}px`;
  circle.classList.add("ripple");
  btn.appendChild(circle);
  setTimeout(() => circle.remove(), 600);
});

$("pt-filter-input").addEventListener("input", renderTrackerList);
$("pt-refresh-btn").addEventListener("click", () => refreshTracker(true));
$("pt-notifications").addEventListener("change", async (event) => {
  const requested = event.target.checked;
  const result = await window.pywebview.api.set_windows_notifications_enabled(requested);
  if (result.error) {
    event.target.checked = false;
    windowsNotificationsEnabled = false;
    $("pt-notification-note").textContent = result.error;
    return;
  }
  windowsNotificationsEnabled = !!result.windowsNotificationsEnabled;
  $("pt-notification-note").textContent = windowsNotificationsEnabled
    ? "Windows alerts are on. Roblox Radar must stay running to check for changes."
    : "Windows alerts are off.";
});
$("pt-notification-sound").addEventListener("change", async (event) => {
  const result = await window.pywebview.api.set_notification_sound_enabled(event.target.checked);
  notificationSoundEnabled = !!result.notificationSoundEnabled;
});
$("pt-test-notification").addEventListener("click", async () => {
  const result = await window.pywebview.api.send_windows_notification(
    "Roblox Radar notifications",
    "Windows toast notifications are working."
  );
  $("pt-notification-note").textContent = result.error
    ? result.error
    : "Test toast sent. Check Windows notifications if it is not visible.";
});
$("pt-relaunch-notifications").addEventListener("click", async () => {
  const button = $("pt-relaunch-notifications");
  const note = $("pt-notification-note");
  button.disabled = true;
  button.innerHTML = '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14-4L4 9M4 5v4h4M4 13a8 8 0 0 0 14 4l2-2M20 19v-4h-4"/></svg> Restarting...';
  note.textContent = "Starting a fresh Roblox Radar session with Windows toast support...";
  try {
    const result = await window.pywebview.api.relaunch_app();
    if (result?.error) {
      button.disabled = false;
      button.innerHTML = '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14-4L4 9M4 5v4h4M4 13a8 8 0 0 0 14 4l2-2M20 19v-4h-4"/></svg> Relaunch Roblox Radar';
      note.textContent = result.error;
    }
  } catch (error) {
    button.disabled = false;
    button.innerHTML = '<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14-4L4 9M4 5v4h4M4 13a8 8 0 0 0 14 4l2-2M20 19v-4h-4"/></svg> Relaunch Roblox Radar';
    note.textContent = "Automatic relaunch failed. Close and reopen Roblox Radar manually: " + error.message;
  }
});

$("pt-interval").addEventListener("change", async () => {
  const seconds = Number($("pt-interval").value);
  const result = await window.pywebview.api.set_tracker_interval(seconds);
  if (result.error) { $("pt-refresh-note").textContent = result.error; return; }
  scheduleTrackerRefresh();
  $("pt-refresh-note").textContent = `Auto-refresh interval saved: ${seconds} seconds while Roblox Radar is open.`;
});
function scheduleTrackerRefresh() {
  if (trackerPollTimer) clearInterval(trackerPollTimer);
  const seconds = Math.max(30, Number($("pt-interval").value) || 60);
  trackerPollTimer = setInterval(() => {
    if (currentTrackerData.some(user => !user.paused)) refreshTracker(false);
  }, seconds * 1000);
}

$("pt-input").addEventListener("keydown", (e) => { if (e.key === "Enter") $("pt-add").click(); });
$("pt-add").addEventListener("click", async () => {
  const q = $("pt-input").value.trim();
  if (!q) return;
  $("pt-error").textContent = "";
  $("pt-add").disabled = true;
  $("pt-add").textContent = "Adding...";
  try {
    const res = await window.pywebview.api.add_tracked(q);
    if (res.error) $("pt-error").textContent = res.error;
    else { $("pt-input").value = ""; await refreshTracker(false); }
  } catch (e) { $("pt-error").textContent = "Add error: " + e.message; }
  finally { $("pt-add").disabled = false; $("pt-add").textContent = "+ Add to Radar"; }
});
$("pt-clear").addEventListener("click", async () => {
  if (!confirm("Clear all tracked players from radar?")) return;
  await window.pywebview.api.clear_tracked();
  currentTrackerData = [];
  renderTrackerList();
});
async function removePlayer(id) {
  await window.pywebview.api.remove_tracked(id);
  currentTrackerData = currentTrackerData.filter(u => Number(u.id) !== Number(id));
  renderTrackerList();
}
async function togglePlayerPause(id, paused) {
  const result = await window.pywebview.api.toggle_tracked_pause(id, paused);
  if (result.error) { $("pt-refresh-note").textContent = result.error; return; }
  currentTrackerData = currentTrackerData.map(u => Number(u.id) === Number(id) ? {...u, paused: !!paused} : u);
  renderTrackerList();
}

function inspectPlayer(query) {
  document.querySelector('.tab[data-tab="player"]').click();
  $("pf-input").value = query;
  searchProfile(query);
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(String(text));
    const orig = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(() => btn.textContent = orig, 1000);
  } catch {}
}

async function pasteInto(inputId, statusId) {
  const input = $(inputId);
  const status = statusId ? $(statusId) : null;
  let text = "";
  try {
    const nativeClipboard = await window.pywebview.api.get_clipboard_text();
    text = String(nativeClipboard?.text || "").trim();
  } catch (_) {}
  if (!text) {
    try { text = String(await navigator.clipboard.readText()).trim(); } catch (_) {}
  }
  if (text) {
    input.value = text;
    input.dispatchEvent(new Event("input", {bubbles: true}));
    if (status) status.textContent = "Clipboard pasted.";
    input.focus();
    return true;
  }
  if (status) status.textContent = "Could not read the clipboard. Copy a username or User ID first.";
  return false;
}

$("pf-paste").addEventListener("click", () => pasteInto("pf-input", "pf-error"));
$("pt-paste").addEventListener("click", () => pasteInto("pt-input", "pt-error"));

// ---------------------------------------------------------
// SERVER FINDER
// ---------------------------------------------------------
$("sf-find").addEventListener("click", async () => {
  const placeIdRaw = $("sf-place-id").value.trim();
  if (!placeIdRaw) return;
  const match = placeIdRaw.match(/\d+/);
  const placeId = parseInt(match ? match[0] : placeIdRaw, 10);
  if (!placeId) { $("sf-status").textContent = "Invalid Place ID"; return; }

  const sfBtn = $("sf-find");
  const origBtnText = sfBtn.innerHTML;
  sfBtn.disabled = true;
  sfBtn.innerHTML = '<span class="btn-loader-spinner"></span> Scanning Servers…';

  $("sf-status").textContent = "";
  $("sf-results").innerHTML = '<div class="sf-item skeleton" style="height:54px;"></div><div class="sf-item skeleton" style="height:54px;"></div><div class="sf-item skeleton" style="height:54px;"></div>';

  try {
    const res = await window.pywebview.api.fetch_servers(placeId);
    if (res.error) {
      sfBtn.disabled = false; sfBtn.innerHTML = origBtnText; $("sf-status").textContent = res.error; $("sf-results").innerHTML = ""; return;
    }
    $("sf-status").textContent = "";
    const max = parseInt($("sf-max").value, 10) || 20;
    const hideFull = $("sf-hide-full").checked;
    let servers = (res.data || []).filter(s => !hideFull || (s.playing < s.maxPlayers)).slice(0, max);

    if (!servers.length) {
      sfBtn.disabled = false; sfBtn.innerHTML = origBtnText; $("sf-results").innerHTML = `<div class="empty-state"><div class="empty-state-icon">${ICON.radar}</div><div class="empty-state-title">No matching servers</div><div class="empty-state-desc">Try unchecking 'Hide Full Servers' or search for another Place ID.</div></div>`; return;
    }

    sfBtn.disabled = false;
    sfBtn.innerHTML = origBtnText;
    $("sf-results").innerHTML = servers.map((s, idx) => {
      const maxP = Math.min(s.maxPlayers || 20, 20);
      const ratio = (s.playing || 0) / (s.maxPlayers || 20);
      const dotType = ratio >= 0.85 ? "full" : (ratio >= 0.4 ? "half" : "empty");
      let dots = "";
      for (let i = 0; i < maxP; i++) {
        dots += `<span class="sf-density-dot ${i < s.playing ? dotType : 'empty'}"></span>`;
      }
      const pingVal = Number(s.ping) || 0;
      const pingPct = Math.min(100, Math.max(8, (pingVal / 250) * 100));
      const pingColor = pingVal < 70 ? "var(--green)" : (pingVal < 140 ? "var(--yellow)" : "var(--red)");
      return `
      <div class="sf-item">
        <div class="sf-info">
          <div><strong>#${idx + 1}</strong></div>
          <div>
            <div>Players: <strong>${s.playing} / ${s.maxPlayers}</strong></div>
            <div class="sf-density-bar" title="${s.playing}/${s.maxPlayers} players">${dots}</div>
          </div>
          <div class="sf-ping-bar-wrap">
            <span>Ping: <strong style="color:${pingColor};">${s.ping ? s.ping + 'ms' : 'N/A'}</strong></span>
            <div class="sf-ping-bar"><div class="sf-ping-fill" style="width:${pingPct}%; background:${pingColor};"></div></div>
          </div>
          <div>FPS: <strong>${s.fps ? Math.round(s.fps) : "N/A"}</strong></div>
        </div>
        <button class="btn small" onclick="joinGame(${placeId}, '${s.id}')">Join Server</button>
      </div>`;
    }).join("");
  } catch (e) {
    $("sf-status").textContent = "Error: " + e.message;
  }
});

$("sf-get-tab").addEventListener("click", async () => {
  const status = $("sf-status");
  try {
    const nativeClipboard = await window.pywebview.api.get_clipboard_text();
    const nativeText = String(nativeClipboard?.text || "").trim();
    if (nativeText) {
      $("sf-place-id").value = nativeText;
      status.textContent = "Clipboard pasted.";
      return;
    }
  } catch (_) {}
  try {
    const txt = String(await navigator.clipboard.readText()).trim();
    if (txt) {
      $("sf-place-id").value = txt;
      status.textContent = "Clipboard pasted.";
      return;
    }
  } catch (_) {}
  status.textContent = "Could not read the clipboard. Copy a Roblox game link or Place ID, then paste it into the field.";
});

function joinGame(placeId, jobId) {
  window.pywebview.api.launch_roblox(placeId, jobId);
}

$('pc-run').addEventListener('click',async()=>{const box=$('pc-result');const left=$('pc-left').value.trim(),right=$('pc-right').value.trim();if(!left||!right){box.innerHTML='<div class="error-msg">Enter both usernames or user IDs to compare.</div>';return}box.innerHTML='<div class="empty-state">Comparing public profile signals…</div>';const r=await window.pywebview.api.compare_profiles(left,right);if(r.error){box.innerHTML=`<div class="error-msg">${escapeHtml(r.error)}</div>`;return}box.innerHTML=(r.profiles||[]).map(p=>{const u=p.user;const avatar=u.avatar||`https://tr.rbxcdn.com/30DAY-AvatarHeadshot-150x150-Png/150/150/AvatarHeadshot/Png/noFilter`;return `<div class="radar-result-card comparison-card"><div class="comparison-head"><img class="comparison-avatar" src="${escapeHtml(avatar)}" alt="${escapeHtml(u.name)} avatar" onerror="this.style.opacity='0.4'"><div><div class="comparison-name">${escapeHtml(u.displayName||u.name)}</div><div class="muted">@${escapeHtml(u.name)} · ID ${u.id}</div><a target="_blank" href="https://www.roblox.com/users/${Number(u.id)}/profile">Open profile ↗</a></div></div><div class="comparison-stats"><div class="comparison-stat"><label>Account created</label><strong>${escapeHtml(p.created||'Unavailable')}</strong></div><div class="comparison-stat"><label>Verified</label><strong>${p.verified==null?'Unavailable':p.verified?'Yes':'No'}</strong></div><div class="comparison-stat"><label>Account state</label><strong>${p.banned==null?'Unavailable':p.banned?'Banned':'Active'}</strong></div><div class="comparison-stat"><label>Friends</label><strong>${radarNum(p.counts.friends)}</strong></div><div class="comparison-stat"><label>Followers</label><strong>${radarNum(p.counts.followers)}</strong></div><div class="comparison-stat"><label>Following</label><strong>${radarNum(p.counts.following)}</strong></div></div></div>`}).join('');});

// -------- Expanded Radar discovery features --------
const radarNum = (v)=>v==null?"—":Number(v).toLocaleString();
function renderGameCard(g, removable=false){const img=g.thumbnail||g.icon||"";return `<div class="radar-exp-card">${img?`<img src="${escapeHtml(img)}">`:''}<div class="exp-meta"><div class="exp-title">${escapeHtml(g.name||"Unknown experience")}</div><div class="exp-stats"><span>▶ ${radarNum(g.playing)} playing</span><span>Visits ${radarNum(g.visits)}</span><span>Favorites ${radarNum(g.favorites)}</span><span>ID ${escapeHtml(String(g.universeId||g.id||""))}</span></div></div><div class="exp-actions">${removable?`<button class="btn danger small" onclick="removeWatchedGame(${Number(g.universeId)})">Remove</button>`:`<button class="btn secondary small" onclick="watchGame('${escapeHtml($('gr-input').value)}')">Watch</button>`}</div></div>`}
async function analyzeGame(){const input=$('gr-input').value.trim();$('gr-result').innerHTML='<div class="empty-state">Fetching public experience data…</div>';const r=await window.pywebview.api.game_radar_lookup(input);if(!r.ok){$('gr-result').innerHTML=`<div class="error-msg">${escapeHtml(r.error||'Lookup failed')}</div>`;return}const g=r.game;$('gr-result').innerHTML=renderGameCard(g);}
async function watchGame(input){const raw=input||$('gr-input').value.trim();const r=await window.pywebview.api.game_watch_add(raw);if(!r.ok){$('gr-result').innerHTML=`<div class="error-msg">${escapeHtml(r.error||'Could not add game')}</div>`;return} $('gr-result').innerHTML='<div class="success-msg">Saved to local watchlist. A snapshot was recorded.</div>'+renderGameCard(r.game);loadGameWatchlist();}
async function loadGameWatchlist(){const r=await window.pywebview.api.game_watch_list();$('gr-watchlist').innerHTML=(r.games||[]).length?(r.games||[]).map(g=>renderGameCard(g,true)).join(''):`<div class="empty-state">No tracked experiences yet. Search a game above and add it.</div>`;}
async function removeWatchedGame(uid){await window.pywebview.api.game_watch_remove(uid);loadGameWatchlist();}
$('gr-search').addEventListener('click',analyzeGame);$('gr-add').addEventListener('click',()=>watchGame());$('gr-refresh').addEventListener('click',loadGameWatchlist);
$('ue-search').addEventListener('click',async()=>{const box=$('ue-results');box.innerHTML='<div class="empty-state">Resolving accounts…</div>';const r=await window.pywebview.api.bulk_user_lookup($('ue-input').value);if(r.error){box.innerHTML=`<div class="error-msg">${escapeHtml(r.error)}</div>`;return}box.innerHTML=(r.users||[]).map(u=>`<div class="radar-result-card bulk-user-card"><img class="bulk-user-avatar" src="https://www.roblox.com/headshot-thumbnail/image?userId=${Number(u.id)}&width=150&height=150&format=png" alt="${escapeHtml(u.name||'User')} avatar" onerror="this.style.display='none'"><h4>${escapeHtml(u.displayName||u.name)}</h4><div class="muted">@${escapeHtml(u.name||'')} · ID ${u.id}</div><div class="exp-actions" style="margin-top:10px"><button class="btn secondary small" onclick="openRobloxProfile(${Number(u.id)})">Profile</button><button class="btn small" onclick="trackResolvedUser(${Number(u.id)})">Track</button></div></div>`).join('')||'<div class="empty-state">No matching users returned.</div>';});
async function trackResolvedUser(uid){const r=await window.pywebview.api.add_tracked(String(uid));if(r&&r.error)alert(r.error);}
function openRobloxProfile(uid){window.open(`https://www.roblox.com/users/${uid}/profile`,'_blank');}
$('gx-search').addEventListener('click',async()=>{const box=$('gx-result');box.innerHTML='<div class="empty-state">Loading public group data…</div>';const raw=$('gx-input').value;const m=raw.match(/groups\/(\d+)/);const gid=m?m[1]:raw;const r=await window.pywebview.api.group_explorer(gid);if(r.error){box.innerHTML=`<div class="error-msg">${escapeHtml(r.error)}</div>`;return}const g=r.group;const groupIcon = g.iconUrl || `https://www.roblox.com/asset/?id=${Number(g.id)}`;
box.innerHTML=`<div class="radar-exp-card group-hero-card"><img class="group-hero-avatar" src="${escapeHtml(groupIcon)}" onerror="this.style.display='none'"><div class="exp-meta"><div class="exp-title" style="font-size:18px;">${escapeHtml(g.name||'Group')}</div><div class="exp-stats"><span class="stat-pill"><span class="dot online"></span> ID: <b>${g.id}</b></span><span class="stat-pill"><span class="dot total"></span> Members: <b>${radarNum(g.memberCount)}</b></span><span class="stat-pill"><span class="dot instudio"></span> Owner: <b>${escapeHtml(g.owner?.username||g.owner?.name||'Unknown')}</b></span></div><p style="margin:10px 0 12px; line-height:1.6; color:#ccc;">${escapeHtml(g.description||'No description returned.')}</p><a class="btn small" target="_blank" href="https://www.roblox.com/communities/${g.id}">Open on Roblox ↗</a></div></div><h3 style="margin-top:20px;">Group Roles</h3><div class="radar-results">${(r.roles||[]).map(x=>`<div class="radar-result-card"><b>${escapeHtml(x.name)}</b><div class="muted">${radarNum(x.memberCount)} members · rank ${x.rank}</div></div>`).join('')||'<div class="empty-state">Roles unavailable.</div>'}</div><h3>Group Experiences</h3><div class="radar-results">${(r.games||[]).map(x=>`<div class="radar-result-card"><b>${escapeHtml(x.name||'Experience')}</b><div class="muted">${radarNum(x.placeVisits)} visits</div></div>`).join('')||'<div class="empty-state">No public group experiences returned.</div>'}</div>`;});
$('cx-search').addEventListener('click',async()=>{const box=$('cx-results');box.innerHTML='<div class="empty-state"><div class="btn-loader-spinner" style="margin: 0 auto 10px;"></div>Searching public catalog with live prices…</div>';const r=await window.pywebview.api.catalog_search($('cx-input').value);if(r.error){box.innerHTML=`<div class="error-msg">${escapeHtml(r.error)}</div>`;return}box.innerHTML=(r.items||[]).map(i=>{
  const thumb = i.thumbnail || '';
  const priceDisplay = i.price === 0 ? '<span class="catalog-free-badge">FREE</span>' : (i.price != null ? `<span class="catalog-price">⏣ ${radarNum(i.price)} Robux</span>` : '<span class="muted">Off-sale / Resale</span>');
  const creatorDisplay = i.creatorName ? `<div class="catalog-creator">By <b>${escapeHtml(i.creatorName)}</b></div>` : '';
  const favCount = i.favoriteCount ? `<span class="catalog-favs">★ ${radarNum(i.favoriteCount)}</span>` : '';
  return `<div class="radar-result-card catalog-card">
    <div class="catalog-thumb-wrap">
      ${thumb ? `<img class="catalog-thumb" src="${escapeHtml(thumb)}" alt="${escapeHtml(i.name||'Item')}" loading="lazy">` : '<div class="catalog-thumb-placeholder">📦</div>'}
      ${favCount}
    </div>
    <div class="catalog-info">
      <h4 class="catalog-title" title="${escapeHtml(i.name||'Item')}">${escapeHtml(i.name||'Catalog item')}</h4>
      ${creatorDisplay}
      <div class="catalog-price-row">${priceDisplay}</div>
      <div class="catalog-actions">
        <a class="btn small" target="_blank" href="https://www.roblox.com/catalog/${Number(i.id)}">View Item ↗</a>
      </div>
    </div>
  </div>`;
}).join('')||'<div class="empty-state">No catalog items returned.</div>';});

// ---------------------------------------------------------
// Startup
// ---------------------------------------------------------
ready().then(async () => {
  const settings = await window.pywebview.api.get_tracker_settings();
  $("pt-interval").value = String(settings.refreshIntervalSeconds || 60);
  windowsNotificationsEnabled = !!settings.windowsNotificationsEnabled && !!settings.windowsNotificationsSupported;
  $("pt-notifications").checked = windowsNotificationsEnabled;
  notificationSoundEnabled = !!settings.notificationSoundEnabled;
  $("pt-notification-sound").checked = notificationSoundEnabled;
  const relaunchBanner = $("pt-notification-restart-banner");
  const relaunchButton = $("pt-relaunch-notifications");
  const needsNotificationRestart = !!settings.windowsNotificationsNeedRestart && !!settings.windowsNotificationsSupported;
  if (needsNotificationRestart) {
    relaunchBanner.style.display = "flex";
    relaunchButton.style.display = "inline-flex";
  }
  if (!settings.windowsNotificationsSupported) {
    // Toast controls are Windows-only. Avoid showing Windows-specific warnings
    // to users running another operating system.
    const notificationRow = $("pt-notifications").closest(".tracker-notification-row");
    if (notificationRow) notificationRow.style.display = "none";
  } else {
    $("pt-notifications").disabled = false;
    $("pt-test-notification").disabled = false;
    $("pt-notification-note").textContent = needsNotificationRestart
      ? "winotify was just installed. Relaunch Roblox Radar once to initialize Windows toast notifications."
      : (windowsNotificationsEnabled
          ? "Windows alerts are on. Roblox Radar must stay running to check for changes."
          : "Off by default. Enable alerts here; checks stop when the app is closed.");
  }
  scheduleTrackerRefresh();
  loadGameWatchlist();
  // Initial load is done now; entering the tracker tab also refreshes immediately,
  // with backend cooldown protection preventing repeated API requests.
  await refreshTracker(false);
});
</script>
</body>
</html>
'''

def main():
    api = RobloxAPI()
    window = webview.create_window(
        title="Roblox Radar - GlobalStats Edition",
        html=HTML_UI,
        js_api=api,
        width=1050,
        height=740,
        min_size=(800, 600)
    )
    api._window = window
    webview.start(debug=False)

if __name__ == "__main__":
    main()
