#!/usr/bin/env python3
"""
Gamer-Friends-Sync
==================

SCRIPT HIGHLIGHTS
- Exports your PlayStation Network (PSN) and Xbox Live friends lists to
  timestamped CSV files.
- Tracks changes between runs (who was added, who was removed) in a local
  snapshot plus an append-only history log, so a scheduled run tells you what
  changed instead of just re-dumping the same list.
- Regenerates a single, self-contained HTML viewer (no CDN, works offline)
  with a live search box, platform filter, and per-friend "seen" toggles.
- Optionally posts a Discord alert (via a webhook) whenever a friend is added
  or removed. Alerts fire only on a real change, never on an unchanged run.
- Reads credentials from environment variables or a local, un-synced JSON
  file. Nothing sensitive is stored inside this script.
- --self-test runs the whole pipeline offline with clearly-labeled synthetic
  data, so you can prove it works before wiring in real credentials.

REQUIREMENTS
- Python 3.9 or newer.
- pip install requests psnawp xbox-webapi
  (psnawp for PlayStation, xbox-webapi for Xbox, requests for Discord alerts)
- PlayStation: a PSN NPSSO token (see the README).
- Xbox: a Microsoft / Entra app client ID and secret, authorized once with
  `--auth-xbox`. No third-party service is involved.

DISCLAIMER
- This tool only READS your own friends lists and writes local files. It never
  posts, deletes, or changes anything on either gaming network.
- PSN NPSSO tokens expire (roughly every two months). When PSN auth fails the
  script says so plainly and keeps going with whatever else is configured.
- Xbox authenticates with your own Microsoft account via xbox-webapi; the raw
  People payload is saved on every run so any model change is visible.

Last Updated: 2026-09-18
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# requests is required for the Xbox path and is a tiny, ubiquitous dependency.
try:
    import requests
except ImportError:  # pragma: no cover - environment guard
    print("Missing dependency 'requests'. Install with: pip install requests", file=sys.stderr)
    raise

# psnawp is imported lazily inside fetch_psn_friends() so that an Xbox-only or
# --self-test run does not require it to be installed.

# --------------------------------------------------------------------------- #
#                               CONFIGURATION                                  #
# --------------------------------------------------------------------------- #

# Xbox: OAuth redirect URI registered on your Microsoft / Entra app, and the
# default filename for the cached, self-refreshing token (kept beside the script).
XBOX_REDIRECT_URI: str = "http://localhost/auth/callback"
XBOX_TOKEN_FILENAME: str = "xbox_tokens.json"
# Xbox Live auth endpoints can be slow; the library default (5s) is too tight.
XBOX_HTTP_TIMEOUT: float = 60.0
# Per-request timeout for the PlayStation (psnawp) calls, which have none by default.
PSN_REQUEST_TIMEOUT: float = 30.0

# Placeholder sentinels that must never be treated as real credentials.
PLACEHOLDERS: Set[str] = {
    "",
    "YOUR_64_CHARACTER_NPSSO_TOKEN",
    "YOUR_NPSSO_TOKEN_HERE",
    "YOUR_ENTRA_CLIENT_ID",
    "YOUR_ENTRA_CLIENT_SECRET",
    "YOUR_DISCORD_WEBHOOK_URL",
    "https://discord.com",
    "REPLACE_ME",
}

DEFAULT_TIMEOUT: int = 20  # seconds for any single HTTP call

# Discord embed colors (decimal). PSN blue, Xbox green.
DISCORD_COLOR: Dict[str, int] = {"playstation": 3447003, "xbox": 5763719}
# Discord caps an embed field "value" at 1024 characters; leave headroom.
DISCORD_FIELD_LIMIT: int = 950


# --------------------------------------------------------------------------- #
#                                  HELPERS                                     #
# --------------------------------------------------------------------------- #

def now_stamp() -> str:
    """Filesystem-safe local timestamp, e.g. 20260918-204600."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def now_iso() -> str:
    """ISO 8601 UTC timestamp for logs and the snapshot."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_secret(name: str, config: Dict[str, str]) -> Optional[str]:
    """
    Resolve a secret by name, preferring an environment variable and falling
    back to the local config file. Returns None when unset or still a
    placeholder, so callers can cleanly skip a platform.
    """
    value = os.environ.get(name)
    if value is None:
        value = config.get(name)
    if value is None:
        return None
    value = value.strip()
    if value in PLACEHOLDERS:
        return None
    return value


def read_local_config(script_dir: Path) -> Dict[str, str]:
    """
    Read secrets.local.json sitting next to this script, if present.

    The file is optional. Keeping secrets in a separate, un-synced file (rather
    than in this script) avoids leaking tokens into any cloud-synced folder or
    version control.
    """
    config_path = script_dir / "secrets.local.json"
    if not config_path.exists():
        return {}
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            print(f"Warning: {config_path.name} is not a JSON object; ignoring it.", file=sys.stderr)
            return {}
        # Coerce every value to string so callers get predictable types.
        return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: could not read {config_path.name}: {exc}", file=sys.stderr)
        return {}


def ensure_dir(path: Path) -> None:
    """Create a directory (and parents) if it does not already exist."""
    path.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
#                          PLAYSTATION (PSN) FETCH                             #
# --------------------------------------------------------------------------- #

def _install_requests_timeout(timeout: float) -> None:
    """
    Force a default timeout onto every `requests`-based HTTP call in this
    process. psnawp issues requests with no explicit timeout, so a stalled
    connection would otherwise hang the run forever. Patching HTTPAdapter.send
    is library-agnostic and reliable, unlike socket.setdefaulttimeout, which
    urllib3 ignores when a request passes timeout=None. Idempotent.
    """
    try:
        import requests.adapters
    except ImportError:
        return

    # Store the desired timeout where the wrapper can read it, and patch once.
    requests.adapters.HTTPAdapter._gfs_default_timeout = timeout  # type: ignore[attr-defined]
    if getattr(requests.adapters.HTTPAdapter, "_gfs_timeout_patched", False):
        return

    original_send = requests.adapters.HTTPAdapter.send

    def send_with_timeout(self, request, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = getattr(
                requests.adapters.HTTPAdapter, "_gfs_default_timeout", timeout
            )
        return original_send(self, request, **kwargs)

    requests.adapters.HTTPAdapter.send = send_with_timeout  # type: ignore[assignment]
    requests.adapters.HTTPAdapter._gfs_timeout_patched = True  # type: ignore[attr-defined]


def fetch_psn_friends(npsso: str) -> List[Dict[str, str]]:
    """
    Return the authenticated user's PSN friends as a list of dicts:
        {"stable_id": <account_id>, "display": <online_id>, "account_id", "online_id"}

    The stable identifier for change tracking is account_id (an online_id can be
    changed by its owner). psnawp fetches each friend's profile individually, so:
      - every HTTP call gets a hard timeout (no infinite hang),
      - a single unreadable profile is skipped, not fatal,
      - progress is printed so a large list does not look frozen, and
      - if the friend stream breaks partway, this RAISES rather than returning a
        truncated list, because a partial list would look like mass unfriending
        to the change tracker and could wipe the last-known-good snapshot.

    Concurrency is deliberately not used: psnawp enforces its own rate limit
    (pyrate-limiter), so parallel requests would be throttled or rejected (429).

    Raises RuntimeError on an auth failure (usually an expired NPSSO token) or an
    interrupted fetch.
    """
    try:
        from psnawp_api import PSNAWP  # imported lazily; only needed for PSN
    except ImportError as exc:
        raise RuntimeError(
            "psnawp is not installed. Install with: pip install psnawp"
        ) from exc

    _install_requests_timeout(PSN_REQUEST_TIMEOUT)

    # Authenticate. Map auth failures to a clear, actionable message.
    try:
        psnawp = PSNAWP(npsso)
        client = psnawp.me()  # the authenticated account (a Client object)
    except Exception as exc:  # noqa: BLE001
        message = str(exc).lower()
        if any(t in message for t in ("npsso", "auth", "unauthorized", "401", "403")):
            raise RuntimeError(
                "PSN authentication failed. Your NPSSO token is likely expired "
                "or invalid. Grab a fresh one (see README) and update PSN_NPSSO."
            ) from exc
        raise RuntimeError(f"PSN sign-in failed: {exc}") from exc

    # Iterate the friends generator. Accessing each user's online_id triggers a
    # per-friend profile fetch, so guard each one and track progress.
    try:
        generator = client.friends_list()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"PSN friends list request failed: {exc}") from exc

    friends: List[Dict[str, str]] = []
    processed = 0
    skipped = 0
    while True:
        try:
            user = next(generator)
        except StopIteration:
            break  # completed the full list
        except Exception as exc:  # noqa: BLE001 - stream broke partway through
            raise RuntimeError(
                f"PSN friend fetch was interrupted after {processed} friends "
                f"({exc}). Not saving a partial list, to avoid a false mass "
                "'removed' event. Re-run when your connection is stable."
            ) from exc

        processed += 1
        account_id = str(getattr(user, "account_id", "") or "")
        try:
            online_id = str(getattr(user, "online_id", "") or "")
        except Exception as exc:  # noqa: BLE001 - one bad profile, keep going
            online_id = ""
            skipped += 1
            print(
                f"PlayStation: could not read one profile "
                f"({account_id or 'unknown id'}): {exc}",
                file=sys.stderr,
            )

        stable_id = account_id or online_id
        if not stable_id:
            continue  # nothing usable to key change-tracking on
        friends.append(
            {
                "stable_id": stable_id,
                "display": online_id or account_id or "(unknown)",
                "account_id": account_id,
                "online_id": online_id,
            }
        )
        if processed % 25 == 0:
            print(f"PlayStation: {processed} friends processed...")

    if skipped:
        print(
            f"PlayStation: {skipped} profile(s) had no readable name (kept by ID).",
            file=sys.stderr,
        )
    return friends


# --------------------------------------------------------------------------- #
#                   XBOX (xbox-webapi, direct Microsoft auth)                  #
# --------------------------------------------------------------------------- #
#
# Xbox Live has no public end-user friends API, so we use the xbox-webapi
# library. It authenticates with YOUR Microsoft account through an app you
# register once in Entra / Azure AD, and calls the official People endpoint.
# No third-party service ever holds your data.
#
# One-time setup (see the README): register the app, then run
#     python gamer_friends_sync.py --auth-xbox
# which opens a Microsoft sign-in and writes a refreshable token file. After
# that, scheduled runs refresh the token silently.

def _xbox_parse_auth_code(pasted: str) -> str:
    """
    Pull the OAuth authorization code out of whatever the user pastes: the full
    redirect URL (http://localhost/auth/callback?code=...) or the bare code.
    """
    import urllib.parse

    text = pasted.strip()
    if "code=" in text:
        parsed = urllib.parse.urlparse(text)
        codes = urllib.parse.parse_qs(parsed.query).get("code")
        if codes:
            return codes[0]
        # Paste contained "code=" but not as a clean query string.
        return text.split("code=", 1)[1].split("&", 1)[0]
    return text


async def _xbox_authenticate_async(client_id: str, client_secret: str, token_file: Path) -> None:
    """Interactive one-time OAuth sign-in that writes the token file. Async."""
    import httpx
    from xbox.webapi.authentication.manager import AuthenticationManager
    from xbox.webapi.common.signed_session import SignedSession

    async with SignedSession() as session:
        session.timeout = httpx.Timeout(XBOX_HTTP_TIMEOUT)
        auth_mgr = AuthenticationManager(session, client_id, client_secret, XBOX_REDIRECT_URI)
        auth_url = auth_mgr.generate_authorization_url()
        print("\n1. Open this URL in a browser and sign in to your Microsoft account:\n")
        print(f"   {auth_url}\n")
        print(f"2. After you approve, the browser tries to load {XBOX_REDIRECT_URI}")
        print("   and shows an error page. That is expected.")
        print("3. Copy the FULL address from the browser bar (or just the value")
        print("   after 'code=') and paste it below.\n")
        pasted = input("Paste the redirect URL or code: ").strip()
        code = _xbox_parse_auth_code(pasted)
        if not code:
            raise RuntimeError("No authorization code found in what you pasted.")
        await auth_mgr.request_tokens(code)
        token_file.write_text(auth_mgr.oauth.model_dump_json(), encoding="utf-8")
    print(f"\nSaved Xbox token to: {token_file}")
    print("Keep that file private; it grants access to your Xbox account.")


def authenticate_xbox(client_id: str, client_secret: str, token_file: Path) -> None:
    """Synchronous wrapper for the one-time Xbox OAuth sign-in."""
    import asyncio

    try:
        asyncio.run(_xbox_authenticate_async(client_id, client_secret, token_file))
    except ImportError as exc:
        raise RuntimeError(
            "xbox-webapi is not installed. Install with: pip install xbox-webapi"
        ) from exc


async def _xbox_fetch_async(
    client_id: str, client_secret: str, token_file: Path
) -> Tuple[List[Dict[str, str]], str]:
    """Fetch friends via xbox-webapi. Returns (friends, raw_json_str). Async."""
    import httpx
    from xbox.webapi.api.client import XboxLiveClient
    from xbox.webapi.authentication.manager import AuthenticationManager
    from xbox.webapi.authentication.models import OAuth2TokenResponse
    from xbox.webapi.common.signed_session import SignedSession

    if not token_file.exists():
        raise RuntimeError(
            f"No Xbox token file at {token_file}. Run the one-time sign-in first: "
            "python gamer_friends_sync.py --auth-xbox"
        )

    async with SignedSession() as session:
        session.timeout = httpx.Timeout(XBOX_HTTP_TIMEOUT)
        auth_mgr = AuthenticationManager(session, client_id, client_secret, XBOX_REDIRECT_URI)
        auth_mgr.oauth = OAuth2TokenResponse.model_validate_json(token_file.read_text(encoding="utf-8"))
        try:
            await auth_mgr.refresh_tokens()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Xbox token refresh failed. Your sign-in likely expired. Re-run: "
                "python gamer_friends_sync.py --auth-xbox"
            ) from exc
        # Persist the refreshed token so the next run stays authenticated.
        token_file.write_text(auth_mgr.oauth.model_dump_json(), encoding="utf-8")

        xbl_client = XboxLiveClient(auth_mgr)
        resp = await xbl_client.people.get_friends_own()

    friends: List[Dict[str, str]] = []
    for person in resp.people:
        xuid = str(getattr(person, "xuid", "") or "")
        gamertag = str(
            getattr(person, "gamertag", "")
            or getattr(person, "modern_gamertag", "")
            or getattr(person, "display_name", "")
            or ""
        )
        real_name = str(getattr(person, "real_name", "") or "")
        presence = str(getattr(person, "presence_state", "") or "")
        friends.append(
            {
                "stable_id": xuid or gamertag,
                "display": gamertag or xuid or "(unknown)",
                "xuid": xuid,
                "gamertag": gamertag,
                "real_name": real_name,
                "presence": presence,
            }
        )
    return friends, resp.model_dump_json()


def fetch_xbox_friends(
    client_id: str, client_secret: str, token_file: Path
) -> Tuple[List[Dict[str, str]], str]:
    """
    Return (friends, raw_json_str) for the Microsoft-authenticated account.

    friends carries stable_id (xuid), display (gamertag), xuid, gamertag,
    real_name, presence. raw_json_str is the library's raw People response,
    saved to disk so any model change is visible. Raises RuntimeError with a
    clear message on any failure.
    """
    import asyncio

    try:
        return asyncio.run(_xbox_fetch_async(client_id, client_secret, token_file))
    except RuntimeError:
        raise
    except ImportError as exc:
        raise RuntimeError(
            "xbox-webapi is not installed. Install with: pip install xbox-webapi"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Xbox fetch failed: {exc}") from exc


# --------------------------------------------------------------------------- #
#                                CSV OUTPUT                                    #
# --------------------------------------------------------------------------- #

def write_csv(path: Path, rows: List[Dict[str, str]], fieldnames: List[str]) -> None:
    """Write rows to a UTF-8 CSV. An empty list still writes a header row."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# --------------------------------------------------------------------------- #
#                        SNAPSHOT + CHANGE DETECTION                           #
# --------------------------------------------------------------------------- #

def load_snapshot(path: Path) -> Dict[str, Any]:
    """Load the previous snapshot, or return an empty structure on first run."""
    if not path.exists():
        return {"playstation": {}, "xbox": {}}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {"playstation": {}, "xbox": {}}
        data.setdefault("playstation", {})
        data.setdefault("xbox", {})
        return data
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: snapshot unreadable, treating as first run: {exc}", file=sys.stderr)
        return {"playstation": {}, "xbox": {}}


def save_snapshot(path: Path, snapshot: Dict[str, Any]) -> None:
    """Persist the current snapshot atomically (write temp, then replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2, ensure_ascii=False)
    tmp.replace(path)


def compute_delta(
    previous: Dict[str, str], current: Dict[str, str]
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """
    Compare two {stable_id: display_name} maps and return (added, removed),
    each a list of {"id": stable_id, "name": display_name}.

    A first run (empty previous map) reports no additions, so a brand-new
    install does not falsely announce your entire list as "just added".
    """
    if not previous:
        return [], []
    prev_ids = set(previous.keys())
    curr_ids = set(current.keys())
    added = [{"id": i, "name": current[i]} for i in sorted(curr_ids - prev_ids)]
    removed = [{"id": i, "name": previous[i]} for i in sorted(prev_ids - curr_ids)]
    return added, removed


def append_history(path: Path, platform: str, added: List[Dict[str, str]], removed: List[Dict[str, str]]) -> None:
    """Append one JSON line per change event to the history log."""
    if not added and not removed:
        return
    stamp = now_iso()
    lines: List[str] = []
    for entry in added:
        lines.append(json.dumps({"ts": stamp, "platform": platform, "event": "added", "id": entry["id"], "name": entry["name"]}, ensure_ascii=False))
    for entry in removed:
        lines.append(json.dumps({"ts": stamp, "platform": platform, "event": "removed", "id": entry["id"], "name": entry["name"]}, ensure_ascii=False))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
#                              HTML VIEWER                                     #
# --------------------------------------------------------------------------- #

def render_html(
    generated_at: str,
    psn_state: Dict[str, Any],
    xbox_state: Dict[str, Any],
    recent_changes: List[Dict[str, str]],
    self_test: bool,
) -> str:
    """
    Build a single, self-contained HTML viewer. All data is embedded as a JS
    object; there are no external requests at runtime. Follows the house web
    conventions: system fonts, namespaced+versioned localStorage with an
    in-memory fallback, honest not-connected states, no <select> dropdowns,
    live search, and prefers-color-scheme dark mode with a manual toggle.
    """
    # Data is embedded via json.dumps, which safely escapes the payload. The
    # only place we build display strings is inside JS at render time, where we
    # escape with a helper. Titles/labels below use HTML entities per house style.
    embedded = {
        "generatedAt": generated_at,
        "selfTest": self_test,
        "platforms": {
            "playstation": psn_state,
            "xbox": xbox_state,
        },
        "recentChanges": recent_changes,
    }
    # Split any "</" inside the JSON so it can never terminate the <script> early.
    data_json = json.dumps(embedded, ensure_ascii=True).replace("</", "<\\/")

    banner = ""
    if self_test:
        banner = (
            '<div class="banner">SELF-TEST MODE &#183; the data below is '
            "synthetic sample data, not your real friends lists.</div>"
        )

    # The JS is intentionally dependency-free and defensive around storage.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gamer Friends</title>
<style>
  :root {{
    --bg: #ffffff; --panel: #f4f5f7; --fg: #1a1a1a; --muted: #5b6270;
    --line: #d9dce2; --accent: #2d6cdf; --psn: #0070d1; --xbox: #107c10;
    --added: #107c10; --removed: #c0392b;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f1115; --panel: #171a21; --fg: #e6e6e6; --muted: #9aa2b1;
      --line: #262b35; --accent: #5a8cf0; --psn: #3a9bff; --xbox: #4ac94a;
      --added: #4ac94a; --removed: #ff6b5e;
    }}
  }}
  :root[data-theme="dark"] {{
    --bg: #0f1115; --panel: #171a21; --fg: #e6e6e6; --muted: #9aa2b1;
    --line: #262b35; --accent: #5a8cf0; --psn: #3a9bff; --xbox: #4ac94a;
    --added: #4ac94a; --removed: #ff6b5e;
  }}
  :root[data-theme="light"] {{
    --bg: #ffffff; --panel: #f4f5f7; --fg: #1a1a1a; --muted: #5b6270;
    --line: #d9dce2; --accent: #2d6cdf; --psn: #0070d1; --xbox: #107c10;
    --added: #107c10; --removed: #c0392b;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--fg); min-height: 100dvh; }}
  header {{
    padding: 16px 20px; border-bottom: 1px solid var(--line);
    display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: baseline;
  }}
  h1 {{ font-size: 1.15rem; margin: 0; }}
  .meta {{ color: var(--muted); font-size: 0.82rem; }}
  .banner {{
    background: #8a6d00; color: #fff; padding: 8px 20px; font-size: 0.85rem;
  }}
  .wrap {{ max-width: 1000px; margin: 0 auto; padding: 16px 20px 60px; }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0; align-items: center; }}
  input[type="search"] {{
    flex: 1 1 220px; min-width: 180px; padding: 9px 12px; font-size: 0.95rem;
    border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--fg);
  }}
  .pills {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .pill {{
    padding: 7px 12px; border: 1px solid var(--line); border-radius: 999px;
    background: var(--panel); color: var(--fg); cursor: pointer; font-size: 0.85rem;
  }}
  .pill[aria-pressed="true"] {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
  .toggle {{ margin-left: auto; padding: 7px 12px; border: 1px solid var(--line);
    border-radius: 8px; background: var(--panel); color: var(--fg); cursor: pointer; font-size: 0.85rem; }}
  .changes {{ margin: 8px 0 18px; }}
  .changes h2 {{ font-size: 0.95rem; margin: 0 0 8px; }}
  .change {{ font-size: 0.85rem; padding: 3px 0; color: var(--muted); }}
  .change .added {{ color: var(--added); font-weight: 600; }}
  .change .removed {{ color: var(--removed); font-weight: 600; }}
  .group-title {{ margin: 20px 0 8px; font-size: 0.95rem; display: flex; align-items: center; gap: 8px; }}
  .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
  .dot.psn {{ background: var(--psn); }}
  .dot.xbox {{ background: var(--xbox); }}
  .count {{ color: var(--muted); font-weight: 400; font-size: 0.85rem; }}
  ul.list {{ list-style: none; margin: 0; padding: 0; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }}
  li.row {{ display: flex; align-items: center; gap: 10px; padding: 9px 12px; border-top: 1px solid var(--line); }}
  li.row:first-child {{ border-top: none; }}
  li.row.seen .name {{ text-decoration: line-through; color: var(--muted); }}
  .name {{ font-size: 0.95rem; }}
  .sub {{ color: var(--muted); font-size: 0.78rem; margin-left: 4px; }}
  .seenbox {{ width: 16px; height: 16px; cursor: pointer; }}
  .empty {{ padding: 16px; color: var(--muted); font-size: 0.9rem; background: var(--panel);
    border: 1px dashed var(--line); border-radius: 10px; }}
  footer {{ color: var(--muted); font-size: 0.78rem; margin-top: 28px; }}
</style>
</head>
<body>
{banner}
<header>
  <h1>Gamer Friends</h1>
  <span class="meta" id="genmeta"></span>
</header>
<div class="wrap">
  <div class="controls">
    <input type="search" id="q" placeholder="Search a name...">
    <div class="pills" id="pills">
      <button class="pill" data-plat="all" aria-pressed="true">All</button>
      <button class="pill" data-plat="playstation" aria-pressed="false">PlayStation</button>
      <button class="pill" data-plat="xbox" aria-pressed="false">Xbox</button>
    </div>
    <button class="toggle" id="theme">Toggle theme</button>
  </div>
  <div class="changes" id="changes"></div>
  <div id="results"></div>
  <footer id="footer"></footer>
</div>
<script>
"use strict";
var DATA = {data_json};

// Namespaced, versioned storage with an in-memory fallback so the page never
// crashes where localStorage is blocked (sandbox/private mode).
var STORAGE_KEY = "gamerfriends:v1";
var memoryStore = {{}};
function loadSeen() {{
  try {{
    var raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {{}};
  }} catch (e) {{ return memoryStore; }}
}}
function saveSeen(obj) {{
  memoryStore = obj;
  try {{ localStorage.setItem(STORAGE_KEY, JSON.stringify(obj)); }} catch (e) {{ /* in-memory only */ }}
}}

var state = {{ platform: "all", query: "", seen: loadSeen() }};

function esc(s) {{
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}}

function platformState(key) {{
  var p = DATA.platforms[key];
  if (!p) return {{ connected: false, error: "", friends: [] }};
  return p;
}}

function friendKey(key, f) {{ return key + ":" + (f.stable_id || f.display); }}

function matches(f) {{
  var q = state.query.trim().toLowerCase();
  if (!q) return true;
  var hay = [(f.display || ""), (f.real_name || ""), (f.online_id || ""), (f.gamertag || "")].join(" ").toLowerCase();
  return hay.indexOf(q) !== -1;
}}

function renderGroup(key, label, dotClass) {{
  var ps = platformState(key);
  if (!ps.connected) {{
    var reason = ps.error ? (" &#183; " + esc(ps.error)) : "";
    return '<h2 class="group-title"><span class="dot ' + dotClass + '"></span>' + label +
           '</h2><div class="empty">Not connected' + reason + '</div>';
  }}
  var all = ps.friends || [];
  var shown = all.filter(matches);
  var head = '<h2 class="group-title"><span class="dot ' + dotClass + '"></span>' + label +
             ' <span class="count">' + shown.length + ' of ' + all.length + '</span></h2>';
  if (all.length === 0) {{
    return head + '<div class="empty">No friends returned for this account.</div>';
  }}
  if (shown.length === 0) {{
    return head + '<div class="empty">No matches for your search.</div>';
  }}
  var rows = shown.map(function (f) {{
    var fk = friendKey(key, f);
    var isSeen = !!state.seen[fk];
    var sub = "";
    if (f.real_name) sub += '<span class="sub">' + esc(f.real_name) + '</span>';
    if (f.presence) sub += '<span class="sub">' + esc(f.presence) + '</span>';
    return '<li class="row' + (isSeen ? ' seen' : '') + '" data-fk="' + esc(fk) + '">' +
           '<input class="seenbox" type="checkbox"' + (isSeen ? ' checked' : '') + '>' +
           '<span class="name">' + esc(f.display) + sub + '</span></li>';
  }}).join("");
  return head + '<ul class="list">' + rows + '</ul>';
}}

function renderChanges() {{
  var el = document.getElementById("changes");
  var ch = DATA.recentChanges || [];
  if (!ch.length) {{ el.innerHTML = ""; return; }}
  var items = ch.map(function (c) {{
    var cls = c.event === "added" ? "added" : "removed";
    var sign = c.event === "added" ? "+" : "-";
    var plat = c.platform === "xbox" ? "Xbox" : "PlayStation";
    return '<div class="change"><span class="' + cls + '">' + sign + " " + esc(c.name) +
           '</span> &#183; ' + plat + ' &#183; ' + esc((c.ts || "").replace("T", " ").replace("+00:00", " UTC")) + '</div>';
  }}).join("");
  el.innerHTML = '<h2>Recent changes</h2>' + items;
}}

function render() {{
  document.getElementById("genmeta").innerHTML =
    "Generated " + esc((DATA.generatedAt || "").replace("T", " ").replace("+00:00", " UTC"));
  renderChanges();
  var out = "";
  if (state.platform === "all" || state.platform === "playstation") {{
    out += renderGroup("playstation", "PlayStation", "psn");
  }}
  if (state.platform === "all" || state.platform === "xbox") {{
    out += renderGroup("xbox", "Xbox", "xbox");
  }}
  document.getElementById("results").innerHTML = out;

  var pc = platformState("playstation");
  var xc = platformState("xbox");
  var totals = [];
  if (pc.connected) totals.push((pc.friends || []).length + " PlayStation");
  if (xc.connected) totals.push((xc.friends || []).length + " Xbox");
  document.getElementById("footer").innerHTML =
    (totals.length ? totals.join(" &#183; ") + " friends. " : "") +
    "Seen toggles are saved only in this browser.";
}}

// Event wiring (delegation, so re-rendered rows keep working).
document.getElementById("q").addEventListener("input", function (e) {{
  state.query = e.target.value; render();
}});
document.getElementById("pills").addEventListener("click", function (e) {{
  var btn = e.target.closest(".pill"); if (!btn) return;
  state.platform = btn.getAttribute("data-plat");
  var buttons = document.querySelectorAll("#pills .pill");
  for (var i = 0; i < buttons.length; i++) {{
    buttons[i].setAttribute("aria-pressed", buttons[i] === btn ? "true" : "false");
  }}
  render();
}});
document.getElementById("results").addEventListener("change", function (e) {{
  if (!e.target.classList.contains("seenbox")) return;
  var li = e.target.closest(".row"); if (!li) return;
  var fk = li.getAttribute("data-fk");
  if (e.target.checked) state.seen[fk] = true; else delete state.seen[fk];
  saveSeen(state.seen);
  li.classList.toggle("seen", e.target.checked);
}});
document.getElementById("theme").addEventListener("click", function () {{
  var root = document.documentElement;
  var cur = root.getAttribute("data-theme");
  var next = cur === "dark" ? "light" : (cur === "light" ? "dark" : "dark");
  root.setAttribute("data-theme", next);
}});

render();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
#                             DISCORD ALERTING                                 #
# --------------------------------------------------------------------------- #

def _format_change_lines(changes: List[Dict[str, str]], event: str) -> List[str]:
    """Turn matching change entries into `code`-wrapped display lines."""
    sign = "+" if event == "added" else "-"
    return [f"{sign} `{c['name']}`" for c in changes if c.get("event") == event]


def _clip_to_field(lines: List[str]) -> str:
    """
    Join lines into a single Discord field value under the 1024-char cap,
    appending a truthful "+N more" tail when the list is too long to fit.
    """
    if not lines:
        return "(none)"
    out: List[str] = []
    used = 0
    for index, line in enumerate(lines):
        addition = (len(line) + 1)  # +1 for the newline
        remaining = len(lines) - index
        tail = f"\n+{remaining} more" if remaining > 1 else ""
        if used + addition + len(tail) > DISCORD_FIELD_LIMIT:
            out.append(f"+{remaining} more")
            break
        out.append(line)
        used += addition
    return "\n".join(out)


def build_discord_payload(changes: List[Dict[str, str]], self_test: bool) -> Optional[Dict[str, Any]]:
    """
    Build a Discord webhook payload (one embed per platform that changed).
    Returns None when there is nothing to report, so callers skip the POST.
    """
    if not changes:
        return None

    embeds: List[Dict[str, Any]] = []
    for platform, label in (("playstation", "PlayStation"), ("xbox", "Xbox")):
        plat_changes = [c for c in changes if c.get("platform") == platform]
        if not plat_changes:
            continue
        added_lines = _format_change_lines(plat_changes, "added")
        removed_lines = _format_change_lines(plat_changes, "removed")
        fields: List[Dict[str, Any]] = []
        if added_lines:
            fields.append({"name": f"Added ({len(added_lines)})", "value": _clip_to_field(added_lines), "inline": False})
        if removed_lines:
            fields.append({"name": f"Removed ({len(removed_lines)})", "value": _clip_to_field(removed_lines), "inline": False})
        if not fields:
            continue
        title = f"{label} friends changed"
        if self_test:
            title = "[SELF-TEST] " + title
        embeds.append({"title": title, "color": DISCORD_COLOR.get(platform, 0), "fields": fields})

    if not embeds:
        return None
    return {"username": "Gamer-Friends-Sync", "embeds": embeds}


def send_discord_alert(webhook_url: str, changes: List[Dict[str, str]], self_test: bool) -> bool:
    """
    POST a change alert to a Discord webhook. Returns True on success.

    Never raises: a Discord failure must not fail the export. Handles the
    Discord 429 rate-limit with a single bounded retry.
    """
    payload = build_discord_payload(changes, self_test)
    if payload is None:
        return False

    for attempt in range(2):
        try:
            response = requests.post(webhook_url, json=payload, timeout=DEFAULT_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            print(f"Discord: could not send alert: {exc}", file=sys.stderr)
            return False

        if response.status_code in (200, 204):
            print("Discord: change alert sent.")
            return True
        if response.status_code == 429 and attempt == 0:
            # Respect Discord's retry_after, capped so we never hang a run.
            try:
                retry_after = float(response.json().get("retry_after", 1.0))
            except (ValueError, AttributeError):
                retry_after = 1.0
            wait = min(max(retry_after, 0.0), 5.0)
            print(f"Discord: rate-limited, retrying in {wait:.1f}s.", file=sys.stderr)
            import time
            time.sleep(wait)
            continue
        snippet = response.text[:200].replace("\n", " ")
        print(f"Discord: webhook returned HTTP {response.status_code}: {snippet}", file=sys.stderr)
        return False
    return False


def send_discord_error(
    webhook_url: str, errors: List[Tuple[str, str]], self_test: bool
) -> bool:
    """
    POST a failure alert to Discord so an unattended run that could not reach a
    platform (for example an expired PSN NPSSO or Xbox token) pages you instead
    of failing silently. Never raises. Returns True on success.
    """
    if not errors:
        return False
    title = "Gamer-Friends-Sync could not reach a platform"
    if self_test:
        title = "[SELF-TEST] " + title
    fields = [
        {"name": platform, "value": message[:DISCORD_FIELD_LIMIT], "inline": False}
        for platform, message in errors
    ]
    payload = {
        "username": "Gamer-Friends-Sync",
        "embeds": [{"title": title, "color": 15158332, "fields": fields}],  # red
    }
    try:
        response = requests.post(webhook_url, json=payload, timeout=DEFAULT_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        print(f"Discord: could not send failure alert: {exc}", file=sys.stderr)
        return False
    if response.status_code in (200, 204):
        print("Discord: failure alert sent.")
        return True
    print(f"Discord: failure webhook returned HTTP {response.status_code}.", file=sys.stderr)
    return False


# --------------------------------------------------------------------------- #
#                              SELF-TEST DATA                                  #
# --------------------------------------------------------------------------- #

def self_test_psn() -> List[Dict[str, str]]:
    """Synthetic PSN friends for offline pipeline testing (clearly fake)."""
    return [
        {"stable_id": "1001", "display": "SamplePSN_Alpha", "account_id": "1001", "online_id": "SamplePSN_Alpha"},
        {"stable_id": "1002", "display": "SamplePSN_Bravo", "account_id": "1002", "online_id": "SamplePSN_Bravo"},
        {"stable_id": "1003", "display": "SamplePSN_Charlie", "account_id": "1003", "online_id": "SamplePSN_Charlie"},
    ]


def self_test_xbox() -> Tuple[List[Dict[str, str]], str]:
    """Synthetic Xbox friends for offline pipeline testing (clearly fake)."""
    friends = [
        {"stable_id": "2001", "display": "SampleXbox_One", "xuid": "2001", "gamertag": "SampleXbox_One", "real_name": "Sample Person", "presence": "Online"},
        {"stable_id": "2002", "display": "SampleXbox_Two", "xuid": "2002", "gamertag": "SampleXbox_Two", "real_name": "", "presence": "Offline"},
    ]
    raw = {"people": [{"xuid": f["xuid"], "gamertag": f["gamertag"], "real_name": f["real_name"], "presence_state": f["presence"]} for f in friends]}
    return friends, json.dumps(raw, ensure_ascii=False)


# --------------------------------------------------------------------------- #
#                                  MAIN                                        #
# --------------------------------------------------------------------------- #

def build_platform_state(connected: bool, error: str, friends: List[Dict[str, str]]) -> Dict[str, Any]:
    """Shape a platform block for both the snapshot and the HTML payload."""
    return {"connected": connected, "error": error, "friends": friends}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export and track PlayStation and Xbox friends lists.",
    )
    parser.add_argument(
        "--export-path",
        default=None,
        help="Output directory (default: an 'output' folder next to this script).",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="Skip generating the HTML viewer (CSV and snapshot only).",
    )
    parser.add_argument(
        "--open-report",
        action="store_true",
        help="Open the generated HTML in the default browser when done.",
    )
    parser.add_argument(
        "--platform",
        choices=["both", "psn", "xbox"],
        default="both",
        help="Which platform(s) to export (default: both).",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the full pipeline offline with synthetic data (no credentials needed).",
    )
    parser.add_argument(
        "--auth-xbox",
        action="store_true",
        help="One-time interactive Microsoft sign-in that creates the Xbox token file.",
    )
    parser.add_argument(
        "--xbox-token-file",
        default=None,
        help="Path to the Xbox token file (default: xbox_tokens.json next to this script).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    script_dir = Path(__file__).resolve().parent

    # Resolve output directory.
    if args.export_path:
        out_dir = Path(args.export_path).expanduser().resolve()
    elif args.self_test:
        out_dir = (script_dir / "output_selftest").resolve()
    else:
        out_dir = (script_dir / "output").resolve()
    ensure_dir(out_dir)

    config = read_local_config(script_dir)
    stamp = now_stamp()
    generated_at = now_iso()

    # Resolve the Xbox token file location (refreshable OAuth token cache).
    if args.xbox_token_file:
        xbox_token_file = Path(args.xbox_token_file).expanduser().resolve()
    else:
        xbox_token_file = script_dir / XBOX_TOKEN_FILENAME

    # ---- One-time interactive Xbox sign-in, then exit.
    if args.auth_xbox:
        client_id = load_secret("XBOX_CLIENT_ID", config)
        client_secret = load_secret("XBOX_CLIENT_SECRET", config)
        if not client_id or not client_secret:
            print(
                "Xbox sign-in needs XBOX_CLIENT_ID and XBOX_CLIENT_SECRET set as "
                "environment variables or in secrets.local.json. See the README.",
                file=sys.stderr,
            )
            return 1
        try:
            authenticate_xbox(client_id, client_secret, xbox_token_file)
            return 0
        except RuntimeError as exc:
            print(f"Xbox sign-in failed: {exc}", file=sys.stderr)
            return 1

    # ---- Fetch each platform, keeping one platform's failure from killing the other.
    psn_error = ""
    xbox_error = ""
    psn_friends: List[Dict[str, str]] = []
    xbox_friends: List[Dict[str, str]] = []
    xbox_raw: str = ""
    psn_connected = False
    xbox_connected = False

    if args.self_test:
        psn_friends = self_test_psn()
        xbox_friends, xbox_raw = self_test_xbox()
        psn_connected = True
        xbox_connected = True
        print("Self-test: using synthetic data for both platforms.")
        # Seed a differing "previous" snapshot so the real delta engine produces
        # visible changes to demonstrate (and to exercise the Discord path).
        save_snapshot(
            out_dir / "snapshot.json",
            {
                "playstation": {"1001": "SamplePSN_Alpha", "1002": "SamplePSN_Bravo", "9999": "SamplePSN_Removed"},
                "xbox": {"2001": "SampleXbox_One"},
                "updated": generated_at,
            },
        )
    else:
        want_psn = args.platform in ("both", "psn")
        want_xbox = args.platform in ("both", "xbox")

        npsso = load_secret("PSN_NPSSO", config) if want_psn else None
        xbox_client_id = load_secret("XBOX_CLIENT_ID", config) if want_xbox else None
        xbox_client_secret = load_secret("XBOX_CLIENT_SECRET", config) if want_xbox else None
        xbox_configured = bool(xbox_client_id and xbox_client_secret)

        if not ((want_psn and npsso) or (want_xbox and xbox_configured)):
            print(
                "No credentials for the selected platform(s). Set PSN_NPSSO, "
                "and/or XBOX_CLIENT_ID + XBOX_CLIENT_SECRET, as environment "
                "variables or in secrets.local.json next to this script. See README.",
                file=sys.stderr,
            )
            return 1

        if want_psn:
            if npsso:
                try:
                    psn_friends = fetch_psn_friends(npsso)
                    psn_connected = True
                    print(f"PlayStation: {len(psn_friends)} friends.")
                except RuntimeError as exc:
                    psn_error = str(exc)
                    print(f"PlayStation: {psn_error}", file=sys.stderr)
            else:
                print("PlayStation: skipped (PSN_NPSSO not set).")

        if want_xbox:
            if xbox_configured:
                try:
                    xbox_friends, xbox_raw = fetch_xbox_friends(
                        xbox_client_id or "", xbox_client_secret or "", xbox_token_file
                    )
                    xbox_connected = True
                    print(f"Xbox: {len(xbox_friends)} friends.")
                    # Save the raw People payload for troubleshooting / field checks.
                    if xbox_raw:
                        (out_dir / f"xbox_raw_{stamp}.json").write_text(xbox_raw, encoding="utf-8")
                except RuntimeError as exc:
                    xbox_error = str(exc)
                    print(f"Xbox: {xbox_error}", file=sys.stderr)
            else:
                print("Xbox: skipped (XBOX_CLIENT_ID / XBOX_CLIENT_SECRET not set).")

    # ---- Write per-platform CSVs (only for platforms that actually connected).
    if psn_connected:
        write_csv(
            out_dir / f"psn_friends_{stamp}.csv",
            psn_friends,
            fieldnames=["display", "online_id", "account_id"],
        )
    if xbox_connected:
        write_csv(
            out_dir / f"xbox_friends_{stamp}.csv",
            xbox_friends,
            fieldnames=["display", "gamertag", "xuid", "real_name", "presence"],
        )

    # ---- Change detection against the previous snapshot.
    snapshot_path = out_dir / "snapshot.json"
    history_path = out_dir / "history.jsonl"
    previous = load_snapshot(snapshot_path)

    recent_changes: List[Dict[str, str]] = []

    def maybe_delta(key: str, connected: bool, friends: List[Dict[str, str]]) -> Dict[str, str]:
        """Return the current {id: name} map, logging deltas only if connected."""
        current_map = {f["stable_id"]: f["display"] for f in friends if f.get("stable_id")}
        if connected:
            prev_map = previous.get(key, {}) if isinstance(previous.get(key), dict) else {}
            added, removed = compute_delta(prev_map, current_map)
            append_history(history_path, key, added, removed)
            for entry in added:
                recent_changes.append({"ts": generated_at, "platform": key, "event": "added", "id": entry["id"], "name": entry["name"]})
            for entry in removed:
                recent_changes.append({"ts": generated_at, "platform": key, "event": "removed", "id": entry["id"], "name": entry["name"]})
            if added or removed:
                print(f"{key}: +{len(added)} added, -{len(removed)} removed since last run.")
        return current_map

    psn_map = maybe_delta("playstation", psn_connected, psn_friends)
    xbox_map = maybe_delta("xbox", xbox_connected, xbox_friends)

    # Only overwrite a platform's snapshot when it actually connected, so a
    # transient auth failure does not wipe the last-known-good list (which would
    # then be reported as a mass "removed" event on the next successful run).
    new_snapshot = dict(previous)
    if psn_connected:
        new_snapshot["playstation"] = psn_map
    if xbox_connected:
        new_snapshot["xbox"] = xbox_map
    new_snapshot["updated"] = generated_at
    save_snapshot(snapshot_path, new_snapshot)

    # ---- Discord alert (fires only on a real change).
    webhook = load_secret("DISCORD_WEBHOOK_URL", config)
    if webhook and recent_changes:
        send_discord_alert(webhook, recent_changes, args.self_test)
    elif webhook and not recent_changes:
        print("Discord: configured, no changes to report.")
    elif not webhook and recent_changes:
        print("Discord: skipped (DISCORD_WEBHOOK_URL not set).")

    # ---- Discord failure alert (so a dead token pages you on scheduled runs).
    platform_errors: List[Tuple[str, str]] = []
    if psn_error:
        platform_errors.append(("PlayStation", psn_error))
    if xbox_error:
        platform_errors.append(("Xbox", xbox_error))
    if webhook and platform_errors:
        send_discord_error(webhook, platform_errors, args.self_test)

    # ---- HTML viewer.
    html_path = out_dir / "gamer_friends.html"
    if not args.no_html:
        psn_state = build_platform_state(psn_connected, psn_error, psn_friends)
        xbox_state = build_platform_state(xbox_connected, xbox_error, xbox_friends)
        html_doc = render_html(generated_at, psn_state, xbox_state, recent_changes, args.self_test)
        html_path.write_text(html_doc, encoding="utf-8")
        print(f"HTML viewer: {html_path}")
        if args.open_report:
            open_in_browser(html_path)

    # ---- Exit status: fail if a configured platform errored and none succeeded.
    if not psn_connected and not xbox_connected:
        print("No platform connected successfully.", file=sys.stderr)
        return 1

    print(f"Done. Output in: {out_dir}")
    return 0


def write_json(path: Path, data: Any) -> None:
    """Write a JSON file with UTF-8 and pretty indentation."""
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)


def open_in_browser(path: Path) -> None:
    """Open a local file in the default browser, failing quietly."""
    try:
        import webbrowser
        webbrowser.open(path.as_uri())
    except Exception as exc:  # noqa: BLE001 - non-fatal convenience
        print(f"Could not open browser: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
