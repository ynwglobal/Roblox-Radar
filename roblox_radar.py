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

    def send_windows_notification(self, title, message):
        if os.name != "nt":
            return {"error": "Windows toast notifications are only available on Windows."}
        try:
            from winotify import Notification
            toast = Notification(
                app_id="Roblox Radar",
                title=str(title)[:80],
                msg=str(message)[:220]
            )
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
        presence_data, presence_available = self._request_json(
            "POST", "https://presence.roblox.com/v1/presence/users", json={"userIds": [uid]}
        )
        presence_rows = presence_data.get("userPresences", []) if presence_available else []
        presence = presence_rows[0] if presence_rows and isinstance(presence_rows[0], dict) else {}

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
        worn_data, worn_available = self._request_json(
            "GET", f"https://avatar.roblox.com/v1/users/{uid}/currently-wearing"
        )
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
            "presence_available": presence_available and bool(presence_rows),
            "avatar_head": head_url,
            "avatar_full": full_url,
            "worn_items": worn_items,
            "worn_available": worn_available,
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
                    r = self._request("POST", "https://presence.roblox.com/v1/presence/users", json={"userIds": batch})
                    if r.status_code == 200:
                        for item in r.json().get("userPresences", []):
                            presences[str(item.get("userId"))] = item
                    else:
                        request_error = f"Roblox presence request returned HTTP {r.status_code}. Showing cached status where available."
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
                        transitions.append({
                            "userId": int(uid),
                            "displayName": tracked_user.get("displayName", tracked_user.get("name", "Player")),
                            "name": tracked_user.get("name", ""),
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
                "playing": game.get("playing", 0),
                "visits": game.get("visits", 0),
                "thumbnail": thumb
            }
        except Exception:
            return None

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
  :root {
    --bg: #111119;
    --panel-bg: #181824;
    --card-bg: #202030;
    --card-hover: #28283d;
    --border: #2e2e42;
    --border-bright: #434363;
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
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 13px;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
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
    transition: opacity 0.5s ease, visibility 0.5s ease;
  }
  .splash-overlay.hidden {
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
  }
  .splash-card {
    width: 480px;
    max-width: 90vw;
    background: rgba(24, 24, 38, 0.95);
    border: 1px solid var(--border-bright);
    border-radius: 16px;
    padding: 24px;
    box-shadow: 0 20px 50px rgba(0,0,0,0.6), 0 0 40px var(--accent-glow);
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    backdrop-filter: blur(12px);
  }
  .splash-img {
    width: 240px;
    height: 240px;
    border-radius: 12px;
    object-fit: cover;
    box-shadow: 0 10px 30px rgba(0,0,0,0.5), 0 0 20px var(--cyan-glow);
    margin-bottom: 20px;
    border: 1px solid var(--border);
  }
  .splash-title {
    font-size: 22px;
    font-weight: 800;
    letter-spacing: 1.5px;
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
    margin-bottom: 20px;
  }
  .splash-bar-wrap {
    width: 100%;
    height: 6px;
    background: #111119;
    border-radius: 999px;
    overflow: hidden;
    margin-bottom: 12px;
    border: 1px solid var(--border);
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
  }

  /* Top Navigation */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 24px;
    background: var(--panel-bg);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .brand-group {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .brand-logo {
    width: 28px;
    height: 28px;
    border-radius: 6px;
    background: linear-gradient(135deg, var(--accent) 0%, var(--cyan) 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    font-size: 14px;
    color: #fff;
    box-shadow: 0 0 12px var(--accent-glow);
  }
  .brand-title {
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 0.5px;
  }
  .brand-tag {
    font-size: 10px;
    padding: 2px 6px;
    background: var(--card-bg);
    color: var(--cyan);
    border: 1px solid var(--border-bright);
    border-radius: 4px;
    font-weight: 600;
    letter-spacing: 0.5px;
  }
  .tabs {
    display: flex;
    gap: 8px;
  }
  .tab {
    background: transparent;
    border: none;
    color: var(--muted);
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    border-radius: 8px;
    transition: all 0.2s;
  }
  .tab:hover {
    color: var(--text);
    background: var(--card-bg);
  }
  .tab.active {
    background: var(--accent);
    color: #fff;
    box-shadow: 0 4px 12px var(--accent-glow);
  }
  .header-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .icon-btn {
    background: var(--card-bg);
    border: 1px solid var(--border);
    color: var(--muted);
    width: 32px;
    height: 32px;
    border-radius: 8px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    transition: all 0.2s;
  }
  .icon-btn:hover {
    color: var(--text);
    border-color: var(--border-bright);
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
    animation: fadeIn 0.2s ease;
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .panel-title {
    font-size: 20px;
    font-weight: 700;
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
    letter-spacing: 0.8px;
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
    border-radius: 8px;
    padding: 10px 14px;
    color: var(--text);
    font-size: 13px;
    outline: none;
    transition: border 0.2s;
  }
  input[type="text"]:focus, input[type="number"]:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-glow);
  }
  .btn {
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 8px;
    padding: 10px 18px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
  }
  .btn:hover {
    background: var(--accent-hover);
    box-shadow: 0 4px 12px var(--accent-glow);
  }
  .btn.secondary {
    background: var(--card-bg);
    color: var(--text);
    border: 1px solid var(--border);
  }
  .btn.secondary:hover {
    background: var(--card-hover);
    border-color: var(--border-bright);
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
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 20px;
    align-items: center;
  }
  .pf-avatar-full {
    width: 110px;
    height: 110px;
    border-radius: 12px;
    background: var(--card-bg);
    object-fit: cover;
    border: 1px solid var(--border);
    flex-shrink: 0;
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
  }
  .sub-tab:hover {
    color: var(--text);
    border-color: var(--border-bright);
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
    border-radius: 12px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    position: relative;
    transition: all 0.2s ease;
  }
  .player-card:hover {
    border-color: var(--border-bright);
    background: var(--card-bg);
    transform: translateY(-2px);
    box-shadow: 0 8px 24px rgba(0,0,0,0.3);
  }

  .pc-top {
    display: flex;
    gap: 14px;
    align-items: center;
  }
  .pc-avatar-wrap {
    position: relative;
    width: 56px;
    height: 56px;
    flex-shrink: 0;
  }
  .pc-avatar {
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background: var(--card-bg);
    border: 2px solid var(--border);
    object-fit: cover;
  }
  .pc-status-ring {
    position: absolute;
    bottom: -2px;
    right: -2px;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    border: 2px solid var(--panel-bg);
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
    top: 76px;
    right: 18px;
    z-index: 100000;
    display: flex;
    flex-direction: column;
    gap: 9px;
    width: min(350px, calc(100vw - 28px));
    pointer-events: none;
  }
  .pt-live-alert {
    display: grid;
    grid-template-columns: 34px minmax(0, 1fr) 24px;
    align-items: start;
    gap: 10px;
    padding: 12px 12px 12px 14px;
    background: rgba(24, 24, 38, .97);
    border: 1px solid rgba(46, 204, 113, .38);
    border-left: 3px solid var(--green);
    border-radius: 12px;
    box-shadow: 0 12px 32px rgba(0,0,0,.35);
    backdrop-filter: blur(14px);
    pointer-events: auto;
    animation: ptAlertIn .22s ease-out;
  }
  .pt-live-alert.status-offline { border-color: var(--border-bright); border-left-color: var(--muted); }
  .pt-live-alert.status-game { border-left-color: var(--cyan); border-color: rgba(0,210,255,.3); }
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
</style>
</head>
<body>
<div id="pt-live-alerts" aria-live="polite" aria-relevant="additions" aria-label="Tracked player status updates"></div>

<!-- SPLASH SCREEN OVERLAY -->
<div id="splash-screen" class="splash-overlay">
  <div class="splash-card">
    <img src="data:image/jpeg;base64,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" class="splash-img" alt="GlobalStats Roblox Radar" />
    <div class="splash-title">ROBLOX RADAR</div>
    <div class="splash-tagline">Made by GlobalStats</div>
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
  </div>
  <div class="tabs">
    <button class="tab" data-tab="server">Server Finder</button>
    <button class="tab active" data-tab="player">Player Profile</button>
    <button class="tab" data-tab="tracker">Player Tracker</button>
  </div>
  <div class="header-actions">
    <button class="icon-btn" id="btn-replay-splash" title="View Splash Screen">⚡</button>
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
        <button class="btn" id="pf-find">Search Profile</button>
      </div>
    </div>
    <div id="pf-error" class="error-msg" style="margin-bottom: 16px;"></div>

    <div id="pf-result-container" style="display:none;">
      <!-- Profile Header -->
      <div class="pf-header">
        <img id="pf-avatar-full" class="pf-avatar-full" src="" alt="Avatar" />
        <div class="pf-details">
          <div class="pf-name">
            <span id="pf-display-name"></span>
            <span id="pf-verified" style="display:none; color:var(--cyan); font-size:16px;">☑</span>
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
          <div class="construction-icon">🚧</div>
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
          <span class="construction-badge">🚧 Under Construction</span>
          <span>Game badges are temporarily paused until a stable, unblocked API method is supported without bypassing Roblox security. Official profile badges are displayed below.</span>
        </div>
        <label>OFFICIAL ROBLOX PROFILE BADGES</label>
        <div id="pf-badges-list" class="grid-badges" style="margin-top: 10px;"></div>
      </div>
    </div>
  </section>

  <!-- ENHANCED PLAYER TRACKER PANEL -->
  <section class="panel" id="panel-tracker">
    <div class="tracker-header">
      <div class="panel-title" style="margin-bottom:0;">Player Tracker</div>
      <div class="tracker-controls">
        <button class="btn secondary small" id="pt-refresh-btn">🔄 Refresh All</button>
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
      <button class="btn small" id="pt-relaunch-notifications">↻ Relaunch Roblox Radar</button>
    </div>

    <!-- Quick Add Form -->
    <div class="form-group">
      <label>ADD PLAYER (USERNAME OR USER ID):</label>
      <div class="row">
        <input type="text" id="pt-input" placeholder="e.g. Wolfpaq or 60596019" />
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

    <!-- Tracker List / Grid -->
    <div id="pt-list" class="tracker-grid"></div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
const ready = () => new Promise((res) => window.pywebview?.api ? res() : window.addEventListener("pywebviewready", res, {once:true}));

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
// Splash Screen Handler
// ---------------------------------------------------------
async function runSplash() {
  const splash = $("splash-screen");
  const bar = $("splash-bar");
  const status = $("splash-status");
  splash.classList.remove("hidden");

  bar.style.width = "20%";
  status.textContent = "Calibrating radar sweep & telemetry...";
  await new Promise(r => setTimeout(r, 400));

  bar.style.width = "65%";
  status.textContent = "Loading local JSON caches & radar data...";
  await new Promise(r => setTimeout(r, 500));

  bar.style.width = "100%";
  status.textContent = "Radar systems active. Ready!";
  await new Promise(r => setTimeout(r, 450));

  splash.classList.add("hidden");
}

$("btn-replay-splash").addEventListener("click", () => runSplash());

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
    count.textContent = "";
    moreButton.style.display = "none";
    return;
  }
  if (!names.length) {
    note.textContent = "Roblox returned no previous usernames for this account.";
    list.innerHTML = "";
    count.textContent = "0 previous usernames returned";
  } else {
    note.textContent = "Names are shown oldest first. These are usernames, not display-name history.";
    list.innerHTML = names.map(name =>
      `<li class="username-history-item">@${escapeHtml(name)}</li>`
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
        const r = await window.pywebview.api.add_tracked(String(res.user.id));
        if (r.error) alert(r.error);
        else {
          $("pf-track-btn").textContent = "✓ Tracked!";
          setTimeout(() => $("pf-track-btn").textContent = "+ Track Player", 1500);
        }
      };

      $("pf-bio").textContent = res.bio || "No bio provided.";
      $("pf-verified").style.display = res.verified ? "inline" : "none";
      $("pf-created").textContent = res.created;
      $("pf-count-friends").textContent = formatCount(res.counts.friends);
      $("pf-count-followers").textContent = formatCount(res.counts.followers);
      $("pf-count-following").textContent = formatCount(res.counts.following);
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
      let st = res.presence_available ? "Offline" : "Unavailable";
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
        wList.innerHTML = `<div class="empty-text">${res.worn_available ? "No current outfit items were returned." : "Current outfit data is unavailable."}</div>`;
      } else {
        wList.innerHTML = res.worn_items.map(i => {
          const itemUrl = `https://www.roblox.com/catalog/${encodeURIComponent(i.id)}`;
          const priceText = i.price === null || i.price === undefined
            ? "Price unavailable"
            : `${formatCount(i.price)} Robux`;
          return `
          <div class="fav-card">
            <img src="${i.thumbnail}" alt="${escapeHtml(i.name)}" onerror="this.style.visibility='hidden'">
            <div class="fav-info">
              <div class="fav-name" title="${escapeHtml(i.name)}">${escapeHtml(i.name)}</div>
              <div class="item-card-actions">
                <span class="fav-meta">${escapeHtml(priceText)}</span>
                <a class="btn secondary small" href="${itemUrl}" target="_blank" rel="noopener noreferrer">Open item</a>
              </div>
            </div>
          </div>
        `;
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

async function loadFavorites(uid) {
  const c = $("pf-favs-list");
  c.innerHTML = '<div class="empty-text">Loading favorites and live player counts...</div>';
  const res = await window.pywebview.api.get_user_favorites(uid);
  if (res.error) { c.innerHTML = `<div class="error-msg">${escapeHtml(res.error)}</div>`; return false; }
  if (!res.favorites.length) { c.innerHTML = '<div class="empty-text">No favorite experiences found.</div>'; return true; }
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
  c.innerHTML = '<div class="empty-text">Loading groups...</div>';
  const res = await window.pywebview.api.get_user_groups(uid);
  if (res.error) { c.innerHTML = `<div class="error-msg">${escapeHtml(res.error)}</div>`; return false; }
  if (!res.groups.length) { c.innerHTML = '<div class="empty-text">No public groups found.</div>'; return true; }
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
  c.innerHTML = '<div class="empty-text">Loading profile badges...</div>';
  const res = await window.pywebview.api.get_user_badges(uid);
  if (res.error) { c.innerHTML = `<div class="error-msg">${escapeHtml(res.error)}</div>`; return false; }
  if (!res.badges.length) {
    c.innerHTML = '<div class="empty-text">No official Roblox profile badges found for this account.</div>';
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
    const card = document.createElement("div");
    card.className = "pt-live-alert" + (!isOnline ? " status-offline" : (isGame ? " status-game" : ""));
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
      <button class="pt-live-alert-close" type="button" aria-label="Dismiss notification">×</button>`;
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
    summary.className = "pt-live-alert";
    summary.innerHTML = `<div class="pt-live-alert-icon" aria-hidden="true">↻</div>
      <div class="pt-live-alert-copy"><div class="pt-live-alert-title">${transitions.length - changes.length} more status updates</div>
      <div class="pt-live-alert-message">Open Player Tracker to see the latest statuses.</div></div>
      <button class="pt-live-alert-close" type="button" aria-label="Dismiss notification">×</button>`;
    summary.querySelector("button").addEventListener("click", () => summary.remove());
    host.prepend(summary);
    while (host.children.length > 3) host.lastElementChild.remove();
    window.setTimeout(() => summary.remove(), 5500);
  }
}

async function sendPresenceNotifications(transitions) {
  // In-app alerts are always shown, even when Windows toast notifications are disabled.
  showInAppPresenceAlerts(transitions);
  if (!windowsNotificationsEnabled || !Array.isArray(transitions) || !transitions.length) return;
  for (const change of transitions.slice(0, 5)) {
    try {
      const name = change.displayName || change.name || "A tracked player";
      const result = await window.pywebview.api.send_windows_notification(
        "Roblox Radar · Status changed",
        `${name} is now ${change.to}.`
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
    await sendPresenceNotifications(result.transitions || []);
  } catch (e) {
    $("pt-refresh-note").textContent = "Could not refresh presence: " + e.message;
  } finally {
    if (refreshBtn) refreshBtn.textContent = "🔄 Refresh All";
  }
}

function renderTrackerList() {
  const c = $("pt-list");
  const filterVal = ($("pt-filter-input").value || "").toLowerCase().trim();
  if (!currentTrackerData || currentTrackerData.length === 0) {
    c.innerHTML = '<div class="empty-text" style="grid-column: 1/-1;">No players currently on radar.<br>Add players by Roblox username or User ID above.</div>';
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
    c.innerHTML = '<div class="empty-text" style="grid-column: 1/-1;">No tracked players match your filter query.</div>';
    return;
  }
  c.innerHTML = filtered.map(u => {
    const p = u.presence || {};
    const pType = p.userPresenceType || 0;
    let statusLabel = "Offline", statusClass = "offline", statusRingColor = "var(--muted)", desc = "Not currently active", joinBtn = "";
    if (u.paused) { statusLabel = "Paused"; statusClass = "offline"; statusRingColor = "var(--yellow)"; desc = "Presence checks paused for this player"; }
    else if (pType === 2) {
      statusLabel = "In Game"; statusClass = "ingame"; statusRingColor = "var(--blue)";
      desc = p.lastLocation ? `Playing: ${escapeHtml(p.lastLocation)}` : "Playing an experience";
      if (p.placeId) joinBtn = `<button class="btn small" style="background:var(--blue);" onclick="joinGame(${p.placeId}, '${p.gameId || ""}')">🎮 Join Game</button>`;
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
      <div class="player-card">
        <div class="pc-top">
          <div class="pc-avatar-wrap">
            <img src="${u.avatar || ""}" class="pc-avatar" alt="${escapeHtml(u.displayName)}" onerror="this.style.visibility='hidden'">
            <div class="pc-status-ring dot ${statusClass}" style="background:${statusRingColor}"></div>
          </div>
          <div class="pc-identity">
            <div class="pc-display" title="${escapeHtml(u.displayName)}">${escapeHtml(u.displayName)} ${u.paused ? '<span class="pc-paused-badge">PAUSED</span>' : ''}</div>
            <div class="pc-user"><span>@${escapeHtml(u.name)}</span><span class="pc-id-badge" onclick="copyText('${u.id}', this)">ID: ${u.id}</span></div>
          </div>
        </div>
        <div class="pc-status-box">
          <div class="pc-status-label" style="color:${statusRingColor};"><span class="dot ${statusClass}"></span> ${statusLabel}</div>
          <div class="pc-status-desc" title="${escapeHtml(desc)}">${escapeHtml(desc)}</div>
          <div class="pc-freshness ${stale ? "stale" : ""}">${escapeHtml(updated + staleText)}</div>
        </div>
        <div class="pc-actions">
          ${joinBtn}
          <button class="btn secondary small" onclick="inspectPlayer('${u.id}')">👤 Profile</button>
          <button class="btn secondary small pc-pause-btn" onclick="togglePlayerPause(${u.id}, ${!u.paused})">${u.paused ? "▶ Resume" : "⏸ Pause"}</button>
          <button class="pc-remove-btn" onclick="removePlayer(${u.id})" title="Remove from Tracker">✕</button>
        </div>
      </div>`;
  }).join("");
}

function updateTrackerStats(total, online, ingame, studio, offline) {
  $("pt-stat-total").textContent = total;
  $("pt-stat-online").textContent = online;
  $("pt-stat-ingame").textContent = ingame;
  $("pt-stat-instudio").textContent = studio;
  $("pt-stat-offline").textContent = offline;
}

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
  button.textContent = "↻ Restarting...";
  note.textContent = "Starting a fresh Roblox Radar session with Windows toast support...";
  try {
    const result = await window.pywebview.api.relaunch_app();
    if (result?.error) {
      button.disabled = false;
      button.textContent = "↻ Relaunch Roblox Radar";
      note.textContent = result.error;
    }
  } catch (error) {
    button.disabled = false;
    button.textContent = "↻ Relaunch Roblox Radar";
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

// ---------------------------------------------------------
// SERVER FINDER
// ---------------------------------------------------------
$("sf-find").addEventListener("click", async () => {
  const placeIdRaw = $("sf-place-id").value.trim();
  if (!placeIdRaw) return;
  const match = placeIdRaw.match(/\d+/);
  const placeId = parseInt(match ? match[0] : placeIdRaw, 10);
  if (!placeId) { $("sf-status").textContent = "Invalid Place ID"; return; }

  $("sf-status").textContent = "Searching servers...";
  $("sf-results").innerHTML = "";

  try {
    const res = await window.pywebview.api.fetch_servers(placeId);
    if (res.error) {
      $("sf-status").textContent = res.error;
      return;
    }
    $("sf-status").textContent = "";
    const max = parseInt($("sf-max").value, 10) || 20;
    const hideFull = $("sf-hide-full").checked;
    let servers = (res.data || []).filter(s => !hideFull || (s.playing < s.maxPlayers)).slice(0, max);

    if (!servers.length) {
      $("sf-results").innerHTML = '<div class="empty-text">No matching servers found.</div>';
      return;
    }

    $("sf-results").innerHTML = servers.map((s, idx) => `
      <div class="sf-item">
        <div class="sf-info">
          <div><strong>#${idx + 1}</strong></div>
          <div>Players: <strong>${s.playing} / ${s.maxPlayers}</strong></div>
          <div>Ping: <span style="color:var(--cyan);">${s.ping || "N/A"} ms</span></div>
          <div>FPS: ${s.fps ? Math.round(s.fps) : "N/A"}</div>
        </div>
        <button class="btn small" onclick="joinGame(${placeId}, '${s.id}')">Join Server</button>
      </div>
    `).join("");
  } catch (e) {
    $("sf-status").textContent = "Error: " + e.message;
  }
});

$("sf-get-tab").addEventListener("click", async () => {
  try {
    const txt = await navigator.clipboard.readText();
    if (txt) $("sf-place-id").value = txt;
  } catch {}
});

function joinGame(placeId, jobId) {
  window.pywebview.api.launch_roblox(placeId, jobId);
}

// ---------------------------------------------------------
// Startup
// ---------------------------------------------------------
ready().then(async () => {
  runSplash();
  const settings = await window.pywebview.api.get_tracker_settings();
  $("pt-interval").value = String(settings.refreshIntervalSeconds || 60);
  windowsNotificationsEnabled = !!settings.windowsNotificationsEnabled && !!settings.windowsNotificationsSupported;
  $("pt-notifications").checked = windowsNotificationsEnabled;
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