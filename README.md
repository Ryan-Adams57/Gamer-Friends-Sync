# Gamer-Friends-Sync

Export your **PlayStation Network** and **Xbox Live** friends lists to CSV, track
who gets added or removed over time, and view them in a single self-contained
HTML page. One small Python script, no third-party gaming service, no paywall.

- **PlayStation** via [`psnawp`](https://github.com/isFakeAccount/psnawp) and your own NPSSO token.
- **Xbox** via [`xbox-webapi`](https://github.com/OpenXbox/xbox-webapi-python) and your own Microsoft account (a free Entra app you register once). No `xbl.io`, no phone verification, no subscription.
- **Change tracking**: a local snapshot plus an append-only history log, so each run tells you who was added or removed.
- **HTML viewer**: offline, no CDN, live search, platform filter, per-friend "seen" toggles, light/dark.
- **Optional Discord alerts**: on friend changes, and on auth failures so a dead token pages you instead of failing silently.
- **Read-only**: it never posts, deletes, or changes anything on either network.

---

## Why this exists

There is no clean, current, all-in-one way to export both friends lists.
PlayStation and Xbox neither offer a one-click export, and the common Xbox route
(`xbl.io` / OpenXBL) gates API keys behind phone verification or a paid plan.
This tool avoids that entirely by authenticating against your own accounts.

---

## Requirements

- **Python 3.9+** (tested on 3.13 and 3.14).
- Dependencies: `pip install -r requirements.txt` (`requests`, `psnawp`, `xbox-webapi`).
- A **PSN NPSSO token** for PlayStation, and/or a **Microsoft/Entra app** for Xbox.

> **The single most common setup mistake: the wrong Python.** On Windows you may
> have several Python installs (the Microsoft Store build, a python.org build,
> the App Execution Alias stub). If you `pip install` into one and run the script
> with another, you get `psnawp is not installed` even though you "just installed
> it." **Install and run with the same interpreter.** Check which one you are on
> with `python -c "import sys; print(sys.executable)"`, and use that exact path
> (or `py -3.13` / `py -3.14` consistently) for both `pip install` and running.

---

## Install

```bash
git clone https://github.com/Ryan-Adams57/gamer-friends-sync.git
cd gamer-friends-sync
python -m pip install -r requirements.txt
cp secrets.local.example.json secrets.local.json   # then edit it (see below)
```

Prove the pipeline works before touching credentials:

```bash
python gamer_friends_sync.py --self-test --open-report
```

`--self-test` uses clearly-labeled synthetic data, no credentials or network needed.

---

## Credentials

Put credentials in `secrets.local.json` (copied from the example) **or** as
environment variables. Environment variables win over the file. Any value left as
its placeholder is treated as "not set" and that platform is skipped, so you can
set up one platform without the other.

### PlayStation (NPSSO token)

1. In a browser, sign in at <https://www.playstation.com>.
2. **While still signed in**, open <https://ca.account.sony.com/api/v1/ssocookie> in the same browser.
3. Copy the 64-character value shown as `"npsso"` into `PSN_NPSSO`.

If step 2 shows `{"error":"invalid_grant",...}`, you were not signed in in that
browser, or it is blocking the session cookie. Sign in first, and use a normal
(non-private) window.

> NPSSO tokens expire roughly every two months. When one does, PSN runs fail with
> a clear "token expired" message; repeat these steps and update the value.

### Xbox (your own Microsoft account, via an Entra app)

No third-party service. Register a free app once, then sign in once.

1. Go to <https://entra.microsoft.com> → **Identity → App registrations → New registration**.
2. Name it anything. **Supported account types: "Personal Microsoft accounts only".**
3. **Redirect URI**: platform **Web**, value exactly `http://localhost/auth/callback`. Register. (Entra may warn that it is not HTTPS; `http://localhost` is allowed. Do **not** use "Single-page application" or "Public client".)
4. Copy the **Application (client) ID** into `XBOX_CLIENT_ID`.
5. **Certificates & secrets → New client secret** → copy the secret **Value** (not the Secret **ID** - the Value is the longer string and is shown only once) into `XBOX_CLIENT_SECRET`.
6. You do **not** need to add any API permissions; the sign-in scope is requested at auth time.
7. Run the one-time sign-in:

   ```bash
   python gamer_friends_sync.py --auth-xbox
   ```

   It prints a Microsoft sign-in URL. Open it, approve, let the browser fail to
   load the `localhost` page (expected), then paste the full address-bar URL back.
   This writes `xbox_tokens.json` next to the script. **After this, scheduled runs
   refresh the token on their own.** Use a Microsoft account that is 18+.

### Discord (optional)

Create a webhook (Server Settings → Integrations → Webhooks → New Webhook, copy
the URL) and put it in `DISCORD_WEBHOOK_URL`. You get an alert when a friend is
added or removed, and a separate red alert if a run cannot reach a platform.

---

## Usage

```bash
python gamer_friends_sync.py                 # export configured platforms
python gamer_friends_sync.py --platform psn  # PlayStation only
python gamer_friends_sync.py --platform xbox # Xbox only
python gamer_friends_sync.py --open-report   # open the HTML viewer when done
```

| Flag | Purpose |
| --- | --- |
| `--platform {both,psn,xbox}` | Which platform(s) to export. Default `both`. |
| `--export-path DIR` | Output directory. Default `output/` next to the script. |
| `--no-html` | Skip the HTML viewer (CSV and snapshot only). |
| `--open-report` | Open the viewer in your browser when done. Leave **off** for scheduled runs. |
| `--self-test` | Run the whole pipeline offline with synthetic data. |
| `--auth-xbox` | One-time interactive Microsoft sign-in that creates the Xbox token. |
| `--xbox-token-file PATH` | Override the Xbox token file location. |

### Output

Everything lands in `output/`:

- `psn_friends_<timestamp>.csv`, `xbox_friends_<timestamp>.csv`
- `gamer_friends.html` - the viewer, regenerated each run
- `snapshot.json` - last-known lists (used for change detection)
- `history.jsonl` - append-only log of every add/remove, with timestamps
- `xbox_raw_<timestamp>.json` - the raw Xbox People payload (troubleshooting)

A platform whose auth fails **does not** overwrite its last-known snapshot, so a
temporary outage is never reported as everyone being removed.

---

## Scheduling

Scripts live in `scripts/`.

**Windows** (elevated PowerShell, from the repo folder):

```powershell
.\scripts\Register-GamerFriendsSyncTask.ps1 -BatchPath .\scripts\run_gamer_friends_sync.bat -Time 08:00 -RunNow
```

Creates a daily task named `Gamer-Friends-Sync` that runs whether or not you are
logged on. Remove it with `-Unregister`.

**macOS** (launchd): edit the four `EDIT-ME` paths in
`scripts/com.example.gamer-friends-sync.plist`, then:

```bash
cp scripts/com.example.gamer-friends-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.gamer-friends-sync.plist
```

Scheduled runs never open a browser. Keep secrets in `secrets.local.json` so the
plist carries no tokens.

---

## Security

- `secrets.local.json` and `xbox_tokens.json` hold live credentials. They are in
  `.gitignore`; keep them out of version control and any cloud-synced folder.
- The Xbox token file grants access to your Xbox account. Treat it like a password.
- This tool only reads. It never writes to either gaming network.

---

## Known limits

- **PlayStation caps at 1000 friends** (the API's per-request limit). Above that,
  the list is truncated at 1000.
- **PlayStation is slow for large lists.** `psnawp` fetches each friend's profile
  individually and rate-limits itself, so a few hundred friends take a few
  minutes. The script prints progress and enforces a per-request timeout so it
  cannot hang. Concurrency is deliberately not used because the library's rate
  limiter would throttle or reject it.
- **The first Xbox auth is interactive** (one browser sign-in). Everything after
  refreshes automatically.
- **Xbox field names** come from `xbox-webapi`'s model. If a future version
  renames a field and a CSV column looks empty, compare against the saved
  `xbox_raw_<timestamp>.json` and adjust `_xbox_fetch_async()`.

---

## Roadmap

Reasonable next steps, not yet built: a combined `friends.json` export, opt-in
CSV of only the changes since last run, and additional platforms (Steam has a
real public API and would fit the same shape). Deliberately out of scope: any
write/unfriend actions, a hosted web service, or a database.

---

## License

MIT. See [LICENSE](LICENSE).

This project is not affiliated with Sony, Microsoft, or Discord. "PlayStation"
and "Xbox" are trademarks of their respective owners.
