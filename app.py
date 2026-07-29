import os
import csv
import io
import calendar as calendar_module
import sqlite3
from datetime import datetime, timedelta, date
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, abort, session
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# Shared site password. Change this string to whatever you want, then share
# it with whoever you want to let onto the site (friends, parents, etc).
# Anyone without this password only sees a login page.
# ---------------------------------------------------------------------------
SITE_PASSWORD = os.environ.get("PV_SITE_PASSWORD", "GoTrojans2026")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pvtracker.db")
VIDEO_DIR = os.path.join(BASE_DIR, "static", "uploads", "videos")
PHOTO_DIR = os.path.join(BASE_DIR, "static", "uploads", "photos")

ALLOWED_VIDEO_EXT = {"mp4", "mov", "m4v", "webm", "avi"}
ALLOWED_PHOTO_EXT = {"png", "jpg", "jpeg", "gif"}
ALLOWED_CSV_EXT = {"csv"}

# Preset session types offered in the Category dropdown on both the CSV and
# video upload forms. "Other" reveals a free-text box for anything else.
CATEGORY_OPTIONS = ["Bullpen", "Pulldown", "Game", "Live BP", "Flat Ground", "Practice", "Other"]

# Preset activities offered on a group's throwing calendar.
THROWING_ACTIVITY_OPTIONS = [
    "Bullpen", "Pulldown", "Long Toss", "Flat Ground", "Live BP",
    "Game", "Recovery", "Rest/Off", "Other",
]

# The three throwing types the player-page velocity chart compares side by side.
VELOCITY_CHART_CATEGORIES = ["Bullpen", "Pulldown", "Game"]

# Preferred display order for categories that exist; anything else found in
# the data is appended after these, alphabetically. Used both by the
# leaderboard's session-type dropdown and the player page's per-category
# spreadsheet tables.
CATEGORY_SORT_ORDER = ["Bullpen", "Pulldown", "Game", "Live BP", "Flat Ground", "Practice", "General"]


def _category_sort_key(cat):
    try:
        return (0, CATEGORY_SORT_ORDER.index(cat))
    except ValueError:
        return (1, cat.lower())

# Values that count as "no data" in a CSV cell and get skipped rather than
# imported as a stat.
BLANK_VALUES = {"", "null", "n/a", "na", "-", "--", "none", "nan"}

# Common pitch-type abbreviations, used only for the sample CSVs / docs so
# Reed knows what column names the importer will recognize as pitch velo.
PITCH_TYPES = [
    ("FB", "Four-Seam Fastball"),
    ("SI", "Sinker / Two-Seam"),
    ("CT", "Cutter"),
    ("SL", "Slider"),
    ("SWP", "Sweeper"),
    ("CB", "Curveball"),
    ("CH", "Changeup"),
    ("SPL", "Splitter"),
]

app = Flask(__name__)
app.secret_key = os.environ.get("PV_SECRET_KEY", "pv-baseball-progress-tracker")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB max upload (videos)
app.permanent_session_lifetime = timedelta(days=30)

os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(PHOTO_DIR, exist_ok=True)


# ---------- Database helpers ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            jersey_number TEXT,
            position TEXT,
            grad_year TEXT,
            photo_filename TEXT,
            notes TEXT,
            group_number TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS stat_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            entry_date TEXT NOT NULL,
            category TEXT,
            stat_name TEXT NOT NULL,
            stat_value REAL NOT NULL,
            source_file TEXT,
            imported_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (player_id) REFERENCES players (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            entry_date TEXT NOT NULL,
            title TEXT,
            category TEXT,
            notes TEXT,
            filename TEXT NOT NULL,
            uploaded_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (player_id) REFERENCES players (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            video_id INTEGER,
            commenter_name TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (player_id) REFERENCES players (id) ON DELETE CASCADE,
            FOREIGN KEY (video_id) REFERENCES videos (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS throwing_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_number TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            activity TEXT NOT NULL,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()

    # Migration: add group_number to a players table that existed before this column did.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(players)")}
    if "group_number" not in existing_cols:
        conn.execute("ALTER TABLE players ADD COLUMN group_number TEXT")
        conn.commit()

    conn.close()


def allowed_file(filename, allowed_ext):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_ext


def is_blank(raw_val):
    """Treat empty cells and common null spellings (NULL, N/A, -, none, nan) as no data."""
    return (raw_val or "").strip().lower() in BLANK_VALUES


def normalize_col(name):
    """Loosen a column header for matching: lowercase, strip spaces/underscores/%/#."""
    return (name or "").lower().replace(" ", "").replace("_", "").replace("%", "").replace("#", "")


STRIKES_COL_NAMES = {"strikes", "strike", "numstrikes"}
PITCHES_COL_NAMES = {"pitches", "totalpitches", "pitchcount", "numpitches"}
STRIKE_PCT_COL_NAMES = {"strikepct", "strikepercent", "strikepercentage"}

IP_COL_NAMES = {"ip", "inningspitched", "innings"}
ER_COL_NAMES = {"er", "earnedruns"}
ERA_COL_NAMES = {"era"}
K_COL_NAMES = {"k", "so", "strikeouts", "ks"}

# Innings a game is worth for these rate stats. High school baseball plays
# 7-inning games (not the MLB's 9), so ERA and K/7 are scaled off this instead.
INNINGS_PER_GAME = 7


def parse_innings_pitched(raw_val):
    """Baseball IP notation puts thirds of an inning after the decimal point
    (.1 = one out = 1/3 inning, .2 = two outs = 2/3 inning) - it is NOT a
    literal decimal fraction. "5.1" means 5 1/3 innings, not 5.1 innings."""
    value = float(raw_val.strip())
    whole = int(value)
    frac_digit = round((value - whole) * 10)
    if frac_digit == 1:
        return whole + 1 / 3
    if frac_digit == 2:
        return whole + 2 / 3
    return value


def parse_date(value):
    """Try a handful of common date formats, fall back to today's date."""
    value = (value or "").strip()
    formats = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return datetime.today().strftime("%Y-%m-%d")


def format_comment_time(value):
    """SQLite datetime('now') gives UTC 'YYYY-MM-DD HH:MM:SS'; show something friendlier."""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%b %d, %Y %I:%M %p")
    except (ValueError, TypeError):
        return value


app.jinja_env.filters["friendly_time"] = format_comment_time


# ---------- Password gate ----------

@app.before_request
def require_login():
    if request.endpoint in ("login", "static"):
        return None
    if not session.get("authenticated"):
        return redirect(url_for("login", next=request.path))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == SITE_PASSWORD:
            session.permanent = True
            session["authenticated"] = True
            next_url = request.form.get("next") or url_for("index")
            return redirect(next_url)
        flash("That password isn't right. Try again.", "error")
        return redirect(url_for("login", next=request.form.get("next", "")))

    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- Routes: dashboard ----------

@app.route("/")
def index():
    conn = get_db()
    players = conn.execute(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM videos v WHERE v.player_id = p.id) AS video_count,
               (SELECT COUNT(*) FROM stat_entries s WHERE s.player_id = p.id) AS stat_count,
               (SELECT MAX(entry_date) FROM stat_entries s WHERE s.player_id = p.id) AS last_stat_date,
               (SELECT MAX(entry_date) FROM videos v WHERE v.player_id = p.id) AS last_video_date
        FROM players p
        ORDER BY p.name COLLATE NOCASE ASC
        """
    ).fetchall()
    conn.close()
    return render_template("index.html", players=players)


# ---------- Routes: players ----------

@app.route("/players/add", methods=["GET", "POST"])
def add_player():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Player name is required.", "error")
            return redirect(url_for("add_player"))

        jersey_number = request.form.get("jersey_number", "").strip()
        position = request.form.get("position", "").strip()
        grad_year = request.form.get("grad_year", "").strip()
        group_number = request.form.get("group_number", "").strip()
        notes = request.form.get("notes", "").strip()

        photo_filename = None
        photo = request.files.get("photo")
        if photo and photo.filename and allowed_file(photo.filename, ALLOWED_PHOTO_EXT):
            safe_name = secure_filename(photo.filename)
            photo_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
            photo.save(os.path.join(PHOTO_DIR, photo_filename))

        conn = get_db()
        conn.execute(
            "INSERT INTO players (name, jersey_number, position, grad_year, photo_filename, notes, group_number) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, jersey_number, position, grad_year, photo_filename, notes, group_number),
        )
        conn.commit()
        conn.close()
        flash(f"Added {name} to the roster.", "success")
        return redirect(url_for("index"))

    return render_template("add_player.html")


@app.route("/players/<int:player_id>")
def player_detail(player_id):
    conn = get_db()
    player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    if not player:
        conn.close()
        abort(404)

    stat_rows = conn.execute(
        "SELECT entry_date, category, stat_name, stat_value FROM stat_entries WHERE player_id = ? ORDER BY entry_date ASC, id ASC",
        (player_id,),
    ).fetchall()

    videos = conn.execute(
        "SELECT * FROM videos WHERE player_id = ? ORDER BY entry_date DESC, id DESC",
        (player_id,),
    ).fetchall()

    video_comment_rows = conn.execute(
        "SELECT * FROM comments WHERE player_id = ? AND video_id IS NOT NULL ORDER BY created_at ASC",
        (player_id,),
    ).fetchall()

    general_comments = conn.execute(
        "SELECT * FROM comments WHERE player_id = ? AND video_id IS NULL ORDER BY created_at ASC",
        (player_id,),
    ).fetchall()

    conn.close()

    # Velocity stats (any stat name containing "velo") broken out by throwing
    # type -> stat_name -> category -> list of {date, value}, so the player
    # page can chart Bullpen/Pulldown/Game velo as separate lines, all three
    # throwing types together on one chart per pitch.
    velocity_by_stat = {}
    for row in stat_rows:
        if "velo" in row["stat_name"].lower() and row["category"] in VELOCITY_CHART_CATEGORIES:
            velocity_by_stat.setdefault(row["stat_name"], {}).setdefault(row["category"], []).append(
                {"date": row["entry_date"], "value": row["stat_value"]}
            )

    # One spreadsheet-style pivot table per session type (Bullpen, Pulldown,
    # Game, plus anything else that's been imported): rows are dates,
    # columns are every stat recorded under that category, with a bold AVG
    # row at the bottom for each column - like a coach's spreadsheet.
    # Rows come pre-sorted (entry_date ASC, id ASC), so when two entries
    # collide on the same date/category/stat (e.g. a CSV re-imported by
    # mistake), the most-recently-imported value simply overwrites the cell.
    raw_by_category = {}
    for row in stat_rows:
        cat = row["category"] or "General"
        bucket = raw_by_category.setdefault(cat, {"stat_names": set(), "dates": set(), "cells": {}})
        bucket["stat_names"].add(row["stat_name"])
        bucket["dates"].add(row["entry_date"])
        bucket["cells"].setdefault(row["entry_date"], {})[row["stat_name"]] = row["stat_value"]

    category_tables = []
    for cat in sorted(raw_by_category.keys(), key=_category_sort_key):
        bucket = raw_by_category[cat]
        stat_names = sorted(bucket["stat_names"])
        dates = sorted(bucket["dates"])

        table_rows = []
        for d in dates:
            row_cells = bucket["cells"].get(d, {})
            table_rows.append({"date": d, "values": [row_cells.get(sn) for sn in stat_names]})

        averages = []
        for sn in stat_names:
            vals = [bucket["cells"][d][sn] for d in dates if sn in bucket["cells"].get(d, {})]
            averages.append(round(sum(vals) / len(vals), 2) if vals else None)

        category_tables.append(
            {"category": cat, "stat_names": stat_names, "rows": table_rows, "averages": averages}
        )

    comments_by_video = {}
    for c in video_comment_rows:
        comments_by_video.setdefault(c["video_id"], []).append(c)

    return render_template(
        "player.html",
        player=player,
        category_tables=category_tables,
        velocity_by_stat=velocity_by_stat,
        videos=videos,
        comments_by_video=comments_by_video,
        general_comments=general_comments,
    )


@app.route("/players/<int:player_id>/edit", methods=["GET", "POST"])
def edit_player(player_id):
    conn = get_db()
    player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    if not player:
        conn.close()
        abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Player name is required.", "error")
            conn.close()
            return redirect(url_for("edit_player", player_id=player_id))

        jersey_number = request.form.get("jersey_number", "").strip()
        position = request.form.get("position", "").strip()
        grad_year = request.form.get("grad_year", "").strip()
        group_number = request.form.get("group_number", "").strip()
        notes = request.form.get("notes", "").strip()

        photo_filename = player["photo_filename"]
        photo = request.files.get("photo")
        if photo and photo.filename and allowed_file(photo.filename, ALLOWED_PHOTO_EXT):
            safe_name = secure_filename(photo.filename)
            photo_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
            photo.save(os.path.join(PHOTO_DIR, photo_filename))

        conn.execute(
            """UPDATE players SET name = ?, jersey_number = ?, position = ?, grad_year = ?,
               notes = ?, photo_filename = ?, group_number = ? WHERE id = ?""",
            (name, jersey_number, position, grad_year, notes, photo_filename, group_number, player_id),
        )
        conn.commit()
        conn.close()
        flash(f"Updated {name}.", "success")
        return redirect(url_for("player_detail", player_id=player_id))

    conn.close()
    return render_template("edit_player.html", player=player)


@app.route("/players/<int:player_id>/delete", methods=["POST"])
def delete_player(player_id):
    conn = get_db()
    conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
    conn.commit()
    conn.close()
    flash("Player removed.", "success")
    return redirect(url_for("index"))


# ---------- Routes: throwing groups / calendars ----------

@app.route("/groups")
def groups_list():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT group_number, COUNT(*) AS player_count
        FROM players
        WHERE group_number IS NOT NULL AND TRIM(group_number) != ''
        GROUP BY group_number
        """
    ).fetchall()
    # Also surface any group that has calendar entries but no players assigned yet.
    entry_groups = conn.execute(
        "SELECT DISTINCT group_number FROM throwing_entries"
    ).fetchall()
    conn.close()

    groups = {r["group_number"]: r["player_count"] for r in rows}
    for r in entry_groups:
        groups.setdefault(r["group_number"], 0)

    group_list = sorted(groups.items(), key=lambda kv: kv[0].lower())
    return render_template("groups.html", group_list=group_list)


@app.route("/groups/<group_number>/calendar")
def group_calendar(group_number):
    group_number = group_number.strip()
    today = date.today()
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
    except ValueError:
        year, month = today.year, today.month
    # Clamp so navigation can't wander into invalid months.
    month = max(1, min(12, month))

    conn = get_db()
    players = conn.execute(
        "SELECT * FROM players WHERE group_number = ? ORDER BY name COLLATE NOCASE ASC",
        (group_number,),
    ).fetchall()

    month_start = f"{year:04d}-{month:02d}-01"
    last_day = calendar_module.monthrange(year, month)[1]
    month_end = f"{year:04d}-{month:02d}-{last_day:02d}"

    entries = conn.execute(
        """SELECT * FROM throwing_entries
           WHERE group_number = ? AND entry_date BETWEEN ? AND ?
           ORDER BY entry_date ASC, id ASC""",
        (group_number, month_start, month_end),
    ).fetchall()
    conn.close()

    entries_by_date = {}
    for e in entries:
        entries_by_date.setdefault(e["entry_date"], []).append(e)

    cal = calendar_module.Calendar(firstweekday=6)  # weeks start Sunday
    weeks = cal.monthdatescalendar(year, month)

    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month % 12 + 1
    next_year = year + 1 if month == 12 else year

    return render_template(
        "group_calendar.html",
        group_number=group_number,
        players=players,
        weeks=weeks,
        entries_by_date=entries_by_date,
        year=year,
        month=month,
        month_name=calendar_module.month_name[month],
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        today_iso=today.strftime("%Y-%m-%d"),
        activity_options=THROWING_ACTIVITY_OPTIONS,
    )


@app.route("/groups/<group_number>/calendar/add", methods=["POST"])
def add_throwing_entry(group_number):
    group_number = group_number.strip()
    entry_date = parse_date(request.form.get("entry_date"))
    activity = request.form.get("activity", "").strip()
    if activity == "Other":
        activity = request.form.get("activity_other", "").strip()
    notes = request.form.get("notes", "").strip()

    if not activity:
        flash("Choose an activity for that throwing day.", "error")
    else:
        conn = get_db()
        conn.execute(
            "INSERT INTO throwing_entries (group_number, entry_date, activity, notes) VALUES (?, ?, ?, ?)",
            (group_number, entry_date, activity, notes),
        )
        conn.commit()
        conn.close()
        flash("Added to the throwing calendar.", "success")

    year, month = entry_date.split("-")[0], entry_date.split("-")[1]
    return redirect(url_for("group_calendar", group_number=group_number, year=int(year), month=int(month)))


@app.route("/throwing/<int:entry_id>/delete", methods=["POST"])
def delete_throwing_entry(entry_id):
    conn = get_db()
    entry = conn.execute("SELECT * FROM throwing_entries WHERE id = ?", (entry_id,)).fetchone()
    if entry:
        conn.execute("DELETE FROM throwing_entries WHERE id = ?", (entry_id,))
        conn.commit()
    conn.close()

    if entry:
        year, month = entry["entry_date"].split("-")[0], entry["entry_date"].split("-")[1]
        flash("Removed from the throwing calendar.", "success")
        return redirect(url_for("group_calendar", group_number=entry["group_number"], year=int(year), month=int(month)))
    return redirect(url_for("groups_list"))


# ---------- Routes: comments ----------

@app.route("/players/<int:player_id>/comments/add", methods=["POST"])
def add_player_comment(player_id):
    commenter_name = request.form.get("commenter_name", "").strip()
    body = request.form.get("body", "").strip()

    if not commenter_name or not body:
        flash("Name and comment are both required.", "error")
        return redirect(url_for("player_detail", player_id=player_id))

    conn = get_db()
    player = conn.execute("SELECT id FROM players WHERE id = ?", (player_id,)).fetchone()
    if not player:
        conn.close()
        abort(404)

    conn.execute(
        "INSERT INTO comments (player_id, video_id, commenter_name, body) VALUES (?, NULL, ?, ?)",
        (player_id, commenter_name, body),
    )
    conn.commit()
    conn.close()
    flash("Comment added.", "success")
    return redirect(url_for("player_detail", player_id=player_id) + "#feedback")


@app.route("/videos/<int:video_id>/comments/add", methods=["POST"])
def add_video_comment(video_id):
    commenter_name = request.form.get("commenter_name", "").strip()
    body = request.form.get("body", "").strip()

    conn = get_db()
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not video:
        conn.close()
        abort(404)

    if not commenter_name or not body:
        flash("Name and comment are both required.", "error")
        conn.close()
        return redirect(url_for("player_detail", player_id=video["player_id"]) + f"#video-{video_id}")

    conn.execute(
        "INSERT INTO comments (player_id, video_id, commenter_name, body) VALUES (?, ?, ?, ?)",
        (video["player_id"], video_id, commenter_name, body),
    )
    conn.commit()
    player_id = video["player_id"]
    conn.close()
    flash("Comment added.", "success")
    return redirect(url_for("player_detail", player_id=player_id) + f"#video-{video_id}")


@app.route("/comments/<int:comment_id>/delete", methods=["POST"])
def delete_comment(comment_id):
    conn = get_db()
    comment = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    if not comment:
        conn.close()
        abort(404)

    player_id = comment["player_id"]
    video_id = comment["video_id"]

    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()
    flash("Comment deleted.", "success")

    if video_id:
        return redirect(url_for("player_detail", player_id=player_id) + f"#video-{video_id}")
    return redirect(url_for("player_detail", player_id=player_id) + "#feedback")


# ---------- Routes: CSV stat upload ----------

@app.route("/upload/csv", methods=["GET", "POST"])
def upload_csv():
    conn = get_db()
    players = conn.execute("SELECT id, name FROM players ORDER BY name COLLATE NOCASE ASC").fetchall()

    if request.method == "POST":
        file = request.files.get("csv_file")
        category = request.form.get("category", "").strip()
        if category == "Other":
            category = request.form.get("category_other", "").strip()
        category = category or "General"

        if not file or not file.filename:
            flash("Please choose a CSV file to upload.", "error")
            conn.close()
            return redirect(url_for("upload_csv"))

        if not allowed_file(file.filename, ALLOWED_CSV_EXT):
            flash("File must be a .csv", "error")
            conn.close()
            return redirect(url_for("upload_csv"))

        # Build a name -> id lookup (case-insensitive)
        name_to_id = {p["name"].strip().lower(): p["id"] for p in players}

        stream = io.StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
        reader = csv.DictReader(stream)

        if not reader.fieldnames:
            flash("Couldn't read any columns from that CSV.", "error")
            conn.close()
            return redirect(url_for("upload_csv"))

        # Identify the player and date columns (case-insensitive match)
        fieldnames = reader.fieldnames
        lower_map = {f.lower().strip(): f for f in fieldnames}
        player_col = lower_map.get("player") or lower_map.get("name")
        date_col = lower_map.get("date")

        if not player_col:
            flash("CSV needs a 'Player' (or 'Name') column so rows can be matched to your roster.", "error")
            conn.close()
            return redirect(url_for("upload_csv"))

        stat_cols = [f for f in fieldnames if f not in (player_col, date_col)]

        # If the CSV has separate Strikes and (Total) Pitches columns but no
        # explicit Strike % column, compute Strike % per row automatically.
        strikes_col = next((c for c in stat_cols if normalize_col(c) in STRIKES_COL_NAMES), None)
        pitches_col = next((c for c in stat_cols if normalize_col(c) in PITCHES_COL_NAMES), None)
        has_explicit_strike_pct = any(normalize_col(c) in STRIKE_PCT_COL_NAMES for c in stat_cols)
        auto_strike_pct = bool(strikes_col and pitches_col and not has_explicit_strike_pct)

        # ERA and K/7 are scaled to a 7-inning high school game (not the
        # MLB's 9), so they're always computed from IP + ER / IP + K rather
        # than trusted from a pre-computed column, which might use the wrong
        # basis. If the CSV also has a raw ERA column, drop it in favor of
        # the one we compute so there's no conflicting duplicate.
        ip_col = next((c for c in stat_cols if normalize_col(c) in IP_COL_NAMES), None)
        er_col = next((c for c in stat_cols if normalize_col(c) in ER_COL_NAMES), None)
        era_col = next((c for c in stat_cols if normalize_col(c) in ERA_COL_NAMES), None)
        k_col = next((c for c in stat_cols if normalize_col(c) in K_COL_NAMES), None)

        auto_era = bool(ip_col and er_col)
        auto_k7 = bool(ip_col and k_col)

        if auto_era and era_col:
            stat_cols = [c for c in stat_cols if c != era_col]

        rows_imported = 0
        rows_skipped = 0
        unmatched_players = set()
        source_file = secure_filename(file.filename)
        # One shared timestamp for every row in this upload, computed once in
        # Python (not via SQL's per-row datetime('now')) so the whole batch
        # can be reliably grouped and deleted together later from Manage Uploads.
        import_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for row in reader:
            raw_name = (row.get(player_col) or "").strip()
            if not raw_name:
                continue
            player_id = name_to_id.get(raw_name.lower())
            if not player_id:
                unmatched_players.add(raw_name)
                rows_skipped += 1
                continue

            entry_date = parse_date(row.get(date_col)) if date_col else datetime.today().strftime("%Y-%m-%d")

            any_stat = False
            for col in stat_cols:
                raw_val = row.get(col)
                if is_blank(raw_val):
                    continue
                cleaned = raw_val.strip().replace("%", "").replace(",", "")
                try:
                    value = float(cleaned)
                except ValueError:
                    continue
                conn.execute(
                    """INSERT INTO stat_entries (player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (player_id, entry_date, category, col.strip(), value, source_file, import_timestamp),
                )
                any_stat = True

            if auto_strike_pct:
                strikes_raw = row.get(strikes_col)
                pitches_raw = row.get(pitches_col)
                if not is_blank(strikes_raw) and not is_blank(pitches_raw):
                    try:
                        strikes_val = float(strikes_raw.strip())
                        pitches_val = float(pitches_raw.strip())
                        if pitches_val > 0:
                            strike_pct = round(strikes_val / pitches_val * 100, 1)
                            conn.execute(
                                """INSERT INTO stat_entries (player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (player_id, entry_date, category, "Strike %", strike_pct, source_file, import_timestamp),
                            )
                            any_stat = True
                    except ValueError:
                        pass

            if auto_era or auto_k7:
                ip_raw = row.get(ip_col)
                if not is_blank(ip_raw):
                    try:
                        ip_val = parse_innings_pitched(ip_raw)
                    except ValueError:
                        ip_val = None

                    if ip_val and ip_val > 0:
                        if auto_era:
                            er_raw = row.get(er_col)
                            if not is_blank(er_raw):
                                try:
                                    er_val = float(er_raw.strip())
                                    era_val = round(er_val / ip_val * INNINGS_PER_GAME, 2)
                                    conn.execute(
                                        """INSERT INTO stat_entries (player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                        (player_id, entry_date, category, "ERA", era_val, source_file, import_timestamp),
                                    )
                                    any_stat = True
                                except ValueError:
                                    pass

                        if auto_k7:
                            k_raw = row.get(k_col)
                            if not is_blank(k_raw):
                                try:
                                    k_val = float(k_raw.strip())
                                    k7_val = round(k_val / ip_val * INNINGS_PER_GAME, 2)
                                    conn.execute(
                                        """INSERT INTO stat_entries (player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                        (player_id, entry_date, category, "K/7", k7_val, source_file, import_timestamp),
                                    )
                                    any_stat = True
                                except ValueError:
                                    pass

            if any_stat:
                rows_imported += 1

        conn.commit()
        conn.close()

        msg = f"Imported stats from {rows_imported} row(s)."
        if rows_skipped:
            msg += f" Skipped {rows_skipped} row(s) with unrecognized players: {', '.join(sorted(unmatched_players))}."
        flash(msg, "success" if rows_imported else "error")
        return redirect(url_for("upload_csv"))

    conn.close()
    return render_template(
        "upload_csv.html", players=players, category_options=CATEGORY_OPTIONS, pitch_types=PITCH_TYPES
    )


# ---------- Routes: leaderboard ----------

@app.route("/leaderboard")
def leaderboard():
    conn = get_db()

    categories = sorted(
        (r["category"] for r in conn.execute(
            "SELECT DISTINCT category FROM stat_entries WHERE category IS NOT NULL AND TRIM(category) != ''"
        )),
        key=_category_sort_key,
    )

    if not categories:
        conn.close()
        return render_template("leaderboard.html", categories=[], stats=[], rows=[], selected=None)

    category = request.args.get("category") or categories[0]
    if category not in categories:
        category = categories[0]

    stats = sorted(
        r["stat_name"] for r in conn.execute(
            "SELECT DISTINCT stat_name FROM stat_entries WHERE category = ?", (category,)
        )
    )

    stat_name = request.args.get("stat") or (stats[0] if stats else None)
    if stat_name not in stats:
        stat_name = stats[0] if stats else None

    metric = request.args.get("metric", "best")
    if metric not in ("best", "recent"):
        metric = "best"

    direction = request.args.get("direction", "desc")
    if direction not in ("asc", "desc"):
        direction = "desc"

    rows = []
    if stat_name:
        entry_rows = conn.execute(
            """SELECT se.player_id, se.entry_date, se.stat_value, p.name, p.jersey_number, p.group_number
               FROM stat_entries se
               JOIN players p ON p.id = se.player_id
               WHERE se.category = ? AND se.stat_name = ?
               ORDER BY se.entry_date ASC, se.id ASC""",
            (category, stat_name),
        ).fetchall()

        by_player = {}
        for r in entry_rows:
            by_player.setdefault(r["player_id"], []).append(r)

        for player_id, entries in by_player.items():
            if metric == "recent":
                chosen = entries[-1]  # last by entry_date/id, since entry_rows is date-ordered ascending
            else:
                if direction == "desc":
                    chosen = max(entries, key=lambda r: (r["stat_value"], r["entry_date"]))
                else:
                    chosen = min(entries, key=lambda r: (r["stat_value"], r["entry_date"]))
            rows.append(chosen)

        rows.sort(key=lambda r: r["stat_value"], reverse=(direction == "desc"))

    conn.close()

    return render_template(
        "leaderboard.html",
        categories=categories,
        stats=stats,
        rows=rows,
        selected={"category": category, "stat": stat_name, "metric": metric, "direction": direction},
    )


# ---------- Routes: video upload ----------

@app.route("/upload/video", methods=["GET", "POST"])
def upload_video():
    conn = get_db()
    players = conn.execute("SELECT id, name FROM players ORDER BY name COLLATE NOCASE ASC").fetchall()

    if request.method == "POST":
        player_id = request.form.get("player_id")
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()
        if category == "Other":
            category = request.form.get("category_other", "").strip()
        notes = request.form.get("notes", "").strip()
        entry_date = parse_date(request.form.get("entry_date"))
        file = request.files.get("video_file")

        if not player_id:
            flash("Please choose a player.", "error")
            conn.close()
            return redirect(url_for("upload_video"))

        if not file or not file.filename or not allowed_file(file.filename, ALLOWED_VIDEO_EXT):
            flash("Please choose a valid video file (mp4, mov, m4v, webm, avi).", "error")
            conn.close()
            return redirect(url_for("upload_video"))

        safe_name = secure_filename(file.filename)
        stored_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
        file.save(os.path.join(VIDEO_DIR, stored_filename))

        conn.execute(
            "INSERT INTO videos (player_id, entry_date, title, category, notes, filename) VALUES (?, ?, ?, ?, ?, ?)",
            (player_id, entry_date, title or safe_name, category, notes, stored_filename),
        )
        conn.commit()
        conn.close()
        flash("Video uploaded.", "success")
        return redirect(url_for("player_detail", player_id=player_id))

    conn.close()
    return render_template("upload_video.html", players=players, category_options=CATEGORY_OPTIONS)


@app.route("/videos/<int:video_id>/delete", methods=["POST"])
def delete_video(video_id):
    conn = get_db()
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if video:
        try:
            os.remove(os.path.join(VIDEO_DIR, video["filename"]))
        except OSError:
            pass
        player_id = video["player_id"]
        conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        conn.commit()
    else:
        player_id = None
    conn.close()
    flash("Video removed.", "success")
    if request.form.get("return_to") == "manage":
        return redirect(url_for("manage_uploads"))
    if player_id:
        return redirect(url_for("player_detail", player_id=player_id))
    return redirect(url_for("index"))


# ---------- Routes: manage uploads (delete CSV imports / videos) ----------

@app.route("/manage")
def manage_uploads():
    conn = get_db()

    import_rows = conn.execute(
        """SELECT source_file, imported_at, category,
                  COUNT(*) AS row_count,
                  COUNT(DISTINCT player_id) AS player_count,
                  MIN(entry_date) AS earliest_date,
                  MAX(entry_date) AS latest_date
           FROM stat_entries
           WHERE source_file IS NOT NULL AND source_file != ''
           GROUP BY source_file, imported_at, category
           ORDER BY imported_at DESC"""
    ).fetchall()

    # Any stat rows with no source_file (shouldn't normally happen, but covers
    # older/edge-case data) get bundled into one "manual entries" bucket per category.
    manual_rows = conn.execute(
        """SELECT category, COUNT(*) AS row_count, COUNT(DISTINCT player_id) AS player_count
           FROM stat_entries
           WHERE source_file IS NULL OR source_file = ''
           GROUP BY category"""
    ).fetchall()

    videos = conn.execute(
        """SELECT v.*, p.name AS player_name
           FROM videos v JOIN players p ON p.id = v.player_id
           ORDER BY v.entry_date DESC, v.id DESC"""
    ).fetchall()

    conn.close()
    return render_template(
        "manage.html", import_rows=import_rows, manual_rows=manual_rows, videos=videos
    )


@app.route("/imports/delete", methods=["POST"])
def delete_import():
    source_file = request.form.get("source_file", "")
    imported_at = request.form.get("imported_at", "")
    category = request.form.get("category", "")

    conn = get_db()
    cur = conn.execute(
        "DELETE FROM stat_entries WHERE source_file = ? AND imported_at = ? AND category = ?",
        (source_file, imported_at, category),
    )
    conn.commit()
    removed = cur.rowcount
    conn.close()

    flash(f"Removed {removed} stat row(s) from {source_file}.", "success")
    return redirect(url_for("manage_uploads"))


if __name__ == "__main__":
    init_db()
    print("\nParadise Valley Baseball Progress Tracker")
    print("Open http://127.0.0.1:5000 in your browser. Press Ctrl+C to stop.\n")
    print(f"Site password: {SITE_PASSWORD}\n")
    app.run(debug=True, host="127.0.0.1", port=5000)
else:
    init_db()
