# Paradise Valley Baseball — Player Progress Tracker

A website for tracking your pitchers' progress over the season: upload stat CSVs or raw TrackMan exports, upload video clips, get feedback from coaches/friends via comments, and see each player's trends, TrackMan reports, and a printable recruiting report on their own page.

## 1. Install (one-time)

You need Python 3.9+ installed. Then, in a terminal, from this folder:

```
pip install -r requirements.txt
```

## 2. Run it

```
python app.py
```

Then open **http://127.0.0.1:5000** in your browser. Leave the terminal window open while you use the site — closing it stops the server. Press `Ctrl+C` in the terminal to stop.

Your data (players, stats, videos, comments, accounts) is saved in this folder — `pvtracker.db` for the database, and `static/uploads/` for videos and photos. Back up this whole folder to back up everything.

## 3. Accounts (replaces the old shared password)

**This is the big change from before.** The site used to sit behind one shared password everyone typed in. It now uses individual accounts instead:

- **First visit ever:** the site shows a one-time setup page where you create the **owner** account (your name + email and/or phone + a password). Nobody else can sign in until you add them.
- **Adding people:** from the **Users** page (only visible to admins), either add someone directly by email/phone — they'll create their own password the first time they sign in — or generate an **invite link** that anyone can open to set themselves up (good for a whole team roster at once). Invite links work for 7 days.
- **Roles:** the **owner** (you) can't be removed or demoted by anyone. **Admins** can manage players, delete videos/comments/imports, and manage other users (but not the owner). **Members** can view everything and post comments, and can optionally be linked to a specific player (handy for a parent or the player themselves — they land straight on that player's page when they sign in).
- **Forgot password:** the Sign In page has a "Forgot your password?" link that texts or emails a 6-digit reset code, if you've set up `PV_SMTP_*` or `PV_TWILIO_*` environment variables (see below). Without those, an admin can reset someone's password from the Users page instead.

Existing site visitors who only knew the old shared password will need to be added as a user (by email or phone) before they can sign back in.

## 4. Using it

**Add players** first (Roster → Add Player). Name, jersey number, position, grad year, group #, contact info, recruiting profile links (Perfect Game / PBR), and a photo are all optional except name. You can edit any player's info later from their page ("Edit Info"), including adding extra contacts (coach, trainer, parent) beyond the player's own info.

**Search the roster**: the Roster page has a live search box (matches name, group #, or grad year as you type) plus a group filter dropdown.

**Import stats** (Import Stats): upload any CSV with a `Player` column (must match a roster name, e.g. "Jake Thompson") and ideally a `Date` column. Every other numeric column is tracked automatically as its own stat — ERA, IP, K, BB, WHIP, pitch count, strikes, velo, or any drill/skills score you use. Blank cells, or cells that just say `NULL`, `N/A`, `-`, or `none`, are skipped rather than imported as zero.

Pick a **session type** on the upload form — Bullpen, Pulldown, Game, Live BP, Flat Ground, Practice, or a custom one — so those stats stay grouped separately on the player page.

**Strike %** is calculated for you: include `Strikes` and `Pitches` (or `Total Pitches`) columns and the importer adds a Strike % stat automatically.

**ERA and K/7** are calculated on the **7-inning high school basis** (not MLB's 9-inning basis): include `IP` and `ER` columns and the importer adds an `ERA` stat, replacing any raw `ERA` column so there's no conflicting duplicate. Include `IP` and `K` columns and it adds `K/7` too. Baseball's innings-pitched notation is handled correctly — `5.1` means 5⅓ innings, not literal tenths.

**Per-pitch velo**: add one column per pitch a pitcher actually throws, e.g. `FB Velo`, `SL Velo`, `CH Velo`. Blank cells are ignored, not treated as a 0 mph pitch.

**Import TrackMan** (Import TrackMan in the nav): upload the raw, unedited TrackMan CSV export (one row per pitch) and it's rolled up automatically into session stats per pitcher — Pitches, Strikes, Strike %, plus top velo and average spin for every pitch type thrown that session. Pitcher names in "Last, First" format are matched to your roster automatically. The full pitch-by-pitch detail (velo, spin, IVB, horizontal break, release point, plate location, tilt, exit velo, launch angle — whatever the export includes) is saved and shown in a **TrackMan Reports** section on the player's page, broken out by pitch type with an expandable pitch-by-pitch table underneath.

You can re-import as often as you like over the season — each upload adds new dated rows, building a history per stat, which is what powers the trend charts on each player's page.

**Add videos** (Add Video): pick a player, date, and one or more files (MP4/MOV/M4V/WEBM/AVI) — select multiple files to upload several clips from the same session at once (they share the date/title/session type/notes). Videos from the same day group into one timeline card with Prev/Next buttons to click through them.

**Comments**: anyone signed in can leave feedback — on a specific video clip or general thoughts on a player (the "Player Feedback" section). Only admins can delete a comment.

**Player pages**: velo stats get a line chart with Bullpen/Pulldown/Game as separate lines. Below that, every stat is a spreadsheet — one table per session type, with a TOT/AVG row: counting stats (IP, H, K, BB, Pitches, Strikes, Outs...) are totaled, and rates (velo, Strike %, ERA, K/7) are averaged. A date range filter (From/To) narrows the charts, tables, videos, and TrackMan reports all at once. There's also a **Printable Report** button (top of the player page) — a clean one-page recruiting summary with contact info, recruiting links, career-best velocities, game totals, and TrackMan pitch summary, ready to print or save as a PDF.

**Groups, calendar, and leaderboard**: give a player a Group # (Add Player or Edit Info). The **Calendar** page has a button per group plus a "General" calendar visible to everyone; click any day to add an event with a message, optional location, and optional other info (hover an event to see the full details, click the × to remove it). The **Leaderboard** can be filtered to one group or left at "All groups," ranking by top velo, average strike %, or total strikeouts.

**Manage Uploads** (admins only): every CSV/TrackMan import is listed with a Delete button that removes just that one import. Below that, every video across the roster with its own Delete button.

## Troubleshooting

**Velo charts look blank:** they load a small charting library from the internet (jsdelivr, with unpkg and cdnjs as backups). If your network blocks all three, the velo cards just show without a chart — nothing else on the page depends on this.

## Notes on video file size

There's no upload size limit on this app (raised to 1 GB per file), but very large video files will take a while to upload and will use disk space on this computer. Periodically check the size of `static/uploads/videos/`.

## Sharing this with your D1 friends, parents, and players

Right now this only runs on your computer (`127.0.0.1` means "this machine only"). To let others reach it from their own devices, it needs to be deployed to a host with **persistent storage** (not a free tier that wipes the filesystem on every restart — that would delete videos and the database). PythonAnywhere's free tier works and is one of the simpler options to set up by hand.

Since the site now uses real accounts instead of a shared password, only people you've explicitly added (or sent an invite link to) can sign in — you control access from the Users page.

## Secrets and config

Set these as **environment variables** on any host you deploy to (rather than leaving the defaults), so a copy of this code on GitHub doesn't reveal your live session key:

- `PV_SECRET_KEY` — used internally by Flask to sign login sessions (defaults to a fixed placeholder string)
- `PV_SMTP_HOST` / `PV_SMTP_PORT` / `PV_SMTP_USER` / `PV_SMTP_PASSWORD` / `PV_SMTP_FROM` — optional, enables "forgot password" reset codes by email (e.g. a Gmail account with an [app password](https://myaccount.google.com/apppasswords))
- `PV_TWILIO_SID` / `PV_TWILIO_TOKEN` / `PV_TWILIO_FROM` — optional, enables reset codes by text message via [Twilio](https://www.twilio.com/)

Without either of these configured, admins can still reset anyone's password by hand from the Users page — resets aren't blocked, just not self-service.

## Putting this on GitHub

This repo is set up to be pushed to GitHub as a **private** repository — `.gitignore` already excludes `pvtracker.db` (your real roster/stats/accounts) and everything in `static/uploads/` (real videos/photos), so that data stays local and never gets committed. Only the app code, templates, styling, and sample CSVs go up.

Pushing to GitHub does not make the site itself viewable to anyone — GitHub only stores code, it doesn't run the Flask app. To let your D1 friends actually use the site, you still need to deploy it to a host as described above.
