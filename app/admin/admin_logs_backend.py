"""
admin_logs_backend.py
=====================
Drop this into your Whitelistv2 Flask app (routes/admin_logs.py or similar).
Register the blueprint in your app factory:

    from routes.admin_logs import admin_logs_bp
    app.register_blueprint(admin_logs_bp)

Requires these DB tables — see create_tables() at the bottom for raw SQL,
or run it once via `flask shell` → `from routes.admin_logs import create_tables; create_tables()`
"""

from flask import Blueprint, request, jsonify, g
from functools import wraps
import sqlite3, os, time, json
from datetime import datetime, timezone

admin_logs_bp = Blueprint("admin_logs", __name__)

# ── DB helper ────────────────────────────────────────────────────────────────
# Reuse however your app exposes a DB connection.
# If you use Flask-SQLAlchemy, swap `get_db()` for `db.session`.

def get_db():
    """Return a sqlite3 connection. Adjust path to match your app."""
    db_path = os.environ.get("DATABASE_PATH", "database.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Auth middleware ───────────────────────────────────────────────────────────

def require_api_key(f):
    """Accepts X-API-Key header OR ?api_key= query param (matches what the FiveM script sends)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = (
            request.headers.get("X-API-Key")
            or request.args.get("api_key")
        )
        expected = os.environ.get("BOT_API_KEY", "")
        if not expected:
            return jsonify({"error": "Server misconfiguration: BOT_API_KEY not set"}), 500
        if key != expected:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def require_session(f):
    """Basic session auth for web-facing endpoints (admin panel)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Plug in your existing session check here.
        # Example if you store user_id in session:
        #   from flask import session
        #   if "user_id" not in session: return redirect("/login")
        #   g.user_id = session["user_id"]
        #   g.is_admin = session.get("is_admin", False)
        # For now we just pass through — replace with your auth.
        return f(*args, **kwargs)
    return decorated


# ── Webhook receiver ──────────────────────────────────────────────────────────

@admin_logs_bp.route("/api/webhooks/trigger", methods=["POST"])
@require_api_key
def webhook_trigger():
    """
    Receives events from gsrp_adminlogs FiveM resource.

    Expected body:
      {
        "event":   "admin_report_created" | "admin_action",
        "version": "1.0",
        "data":    { ... }
      }
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Invalid JSON"}), 400

    event   = body.get("event")
    version = body.get("version", "1.0")
    data    = body.get("data", {})

    if event == "admin_report_created":
        _store_admin_report(data)
    elif event == "admin_action":
        _store_admin_action(data)
    else:
        # Unknown event — store it anyway for debugging
        _store_raw_event(event, version, data)

    return jsonify({"status": "ok"}), 200


def _store_admin_report(data: dict):
    """Full report with resolved user IDs — primary record."""
    db = get_db()
    try:
        admin  = data.get("admin") or {}
        target = data.get("target") or {}
        db.execute(
            """
            INSERT OR IGNORE INTO admin_reports (
                report_id, admin_user_id, target_user_id,
                command, command_label, category, reason,
                is_developer, from_scanner, source_resource,
                admin_name, admin_discord, admin_license, admin_steam, admin_ip, admin_server_id,
                target_name, target_discord, target_license, target_steam, target_server_id,
                timestamp, created_at
            ) VALUES (
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?
            )
            """,
            (
                data.get("reportId"),
                data.get("adminUserId"),
                data.get("targetUserId"),
                data.get("command"),
                data.get("commandLabel"),
                data.get("category"),
                data.get("reason"),
                1 if data.get("isDeveloper") else 0,
                1 if data.get("fromScanner") else 0,
                data.get("sourceResource", "manual"),
                admin.get("name"),
                (admin.get("discord") or "").replace("discord:", ""),
                (admin.get("license") or "").replace("license:", ""),
                admin.get("steam"),
                (admin.get("ip") or "").replace("ip:", ""),
                admin.get("serverId"),
                target.get("name"),
                (target.get("discord") or "").replace("discord:", ""),
                (target.get("license") or "").replace("license:", ""),
                target.get("steam"),
                target.get("serverId"),
                data.get("timestamp"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()
    finally:
        db.close()


def _store_admin_action(data: dict):
    """Lightweight live-feed action — used for real-time dashboard."""
    db = get_db()
    try:
        admin  = data.get("admin") or {}
        target = data.get("target") or {}
        db.execute(
            """
            INSERT OR IGNORE INTO admin_actions (
                action_id, command, command_label, category, reason,
                is_developer, from_scanner, source_resource,
                admin_name, admin_discord, admin_license,
                target_name, target_discord, target_license,
                timestamp, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("id"),
                data.get("command"),
                data.get("label"),
                data.get("category"),
                data.get("reason"),
                1 if data.get("isDeveloper") else 0,
                1 if data.get("fromScanner") else 0,
                data.get("resource", "manual"),
                admin.get("name"),
                (admin.get("discord") or "").replace("discord:", ""),
                (admin.get("license") or "").replace("license:", ""),
                target.get("name"),
                (target.get("discord") or "").replace("discord:", ""),
                (target.get("license") or "").replace("license:", ""),
                data.get("timestamp"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()
    finally:
        db.close()


def _store_raw_event(event, version, data):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO raw_webhook_events (event, version, payload, created_at) VALUES (?, ?, ?, ?)",
            (event, version, json.dumps(data), datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    finally:
        db.close()


# ── User resolution endpoint ──────────────────────────────────────────────────

@admin_logs_bp.route("/api/users/discord/<discord_id>", methods=["GET"])
@require_api_key
def get_user_by_discord(discord_id):
    """
    Resolves a raw Discord ID (with or without 'discord:' prefix) to an internal user ID.
    The FiveM resource calls this before filing a report so it can attach the user's
    website account to the report.

    Adjust the query to match your users table name/column names.
    """
    clean_id = discord_id.replace("discord:", "")
    db = get_db()
    try:
        # ── Adjust this query to match YOUR users table ──
        row = db.execute(
            "SELECT id FROM users WHERE discord_id = ? LIMIT 1",
            (clean_id,)
        ).fetchone()

        if row:
            return jsonify({"id": row["id"]}), 200
        return jsonify({"error": "User not found"}), 404
    finally:
        db.close()


# ── Health check ──────────────────────────────────────────────────────────────

@admin_logs_bp.route("/api/health", methods=["GET"])
@require_api_key
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}), 200


# ── Heartbeat ─────────────────────────────────────────────────────────────────

@admin_logs_bp.route("/api/server/heartbeat", methods=["POST"])
@require_api_key
def server_heartbeat():
    body = request.get_json(silent=True) or {}
    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO server_heartbeats (player_count, max_players, players_json, timestamp, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                body.get("playerCount", 0),
                body.get("maxPlayers", 32),
                json.dumps(body.get("players", [])),
                body.get("timestamp"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "ok"}), 200


# ── Kill stats ────────────────────────────────────────────────────────────────

@admin_logs_bp.route("/api/server/stats", methods=["POST"])
@require_api_key
def server_stats():
    body = request.get_json(silent=True) or {}
    if body.get("type") == "kill":
        killer = body.get("killer") or {}
        victim = body.get("victim") or {}
        db = get_db()
        try:
            db.execute(
                """
                INSERT INTO kill_feed (
                    killer_name, killer_discord, killer_license, killer_server_id,
                    victim_name, victim_discord, victim_license, victim_server_id,
                    weapon, timestamp, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    killer.get("name"),
                    (killer.get("discord") or "").replace("discord:", ""),
                    (killer.get("license") or "").replace("license:", ""),
                    killer.get("serverId"),
                    victim.get("name"),
                    (victim.get("discord") or "").replace("discord:", ""),
                    (victim.get("license") or "").replace("license:", ""),
                    victim.get("serverId"),
                    body.get("weapon"),
                    body.get("timestamp"),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            db.commit()
        finally:
            db.close()
    return jsonify({"status": "ok"}), 200


# ── Disconnect log ────────────────────────────────────────────────────────────

@admin_logs_bp.route("/api/server/disconnect", methods=["POST"])
@require_api_key
def server_disconnect():
    body = request.get_json(silent=True) or {}
    player = body.get("player") or {}
    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO disconnect_log (
                player_name, player_discord, player_license, player_server_id,
                reason, timestamp, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                player.get("name"),
                (player.get("discord") or "").replace("discord:", ""),
                (player.get("license") or "").replace("license:", ""),
                player.get("serverId"),
                body.get("reason"),
                body.get("timestamp"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "ok"}), 200


# ── Web-facing API (for the admin logs page) ──────────────────────────────────

@admin_logs_bp.route("/api/admin-logs", methods=["GET"])
@require_session
def list_admin_logs():
    """
    Paginated list of admin reports.
    Query params:
      page        (default 1)
      per_page    (default 25, max 100)
      category    filter by category
      admin_name  filter by admin name (partial match)
      command     filter by command
    """
    page     = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(1, int(request.args.get("per_page", 25))))
    offset   = (page - 1) * per_page

    filters  = []
    params   = []

    if request.args.get("category"):
        filters.append("category = ?")
        params.append(request.args["category"])
    if request.args.get("admin_name"):
        filters.append("admin_name LIKE ?")
        params.append(f"%{request.args['admin_name']}%")
    if request.args.get("command"):
        filters.append("command = ?")
        params.append(request.args["command"])

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    db = get_db()
    try:
        total = db.execute(f"SELECT COUNT(*) FROM admin_reports {where}", params).fetchone()[0]
        rows  = db.execute(
            f"""
            SELECT * FROM admin_reports
            {where}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            params + [per_page, offset],
        ).fetchall()
        return jsonify({
            "total":    total,
            "page":     page,
            "per_page": per_page,
            "pages":    max(1, -(-total // per_page)),
            "reports":  [dict(r) for r in rows],
        }), 200
    finally:
        db.close()


@admin_logs_bp.route("/api/admin-logs/<report_id>", methods=["GET"])
@require_session
def get_admin_log(report_id):
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM admin_reports WHERE report_id = ?", (report_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify(dict(row)), 200
    finally:
        db.close()


@admin_logs_bp.route("/api/admin-logs/stats/summary", methods=["GET"])
@require_session
def admin_logs_summary():
    """Quick stats for the dashboard header cards."""
    db = get_db()
    try:
        total        = db.execute("SELECT COUNT(*) FROM admin_reports").fetchone()[0]
        today        = db.execute(
            "SELECT COUNT(*) FROM admin_reports WHERE DATE(created_at) = DATE('now')"
        ).fetchone()[0]
        by_category  = db.execute(
            "SELECT category, COUNT(*) as cnt FROM admin_reports GROUP BY category ORDER BY cnt DESC"
        ).fetchall()
        top_admins   = db.execute(
            """
            SELECT admin_name, COUNT(*) as cnt
            FROM admin_reports
            GROUP BY admin_name
            ORDER BY cnt DESC
            LIMIT 5
            """
        ).fetchall()
        return jsonify({
            "total":       total,
            "today":       today,
            "by_category": [dict(r) for r in by_category],
            "top_admins":  [dict(r) for r in top_admins],
        }), 200
    finally:
        db.close()


# ── DB schema creation ────────────────────────────────────────────────────────

def create_tables():
    """Call once to create all required tables."""
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS admin_reports (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id       TEXT    UNIQUE,
            admin_user_id   INTEGER,
            target_user_id  INTEGER,
            command         TEXT,
            command_label   TEXT,
            category        TEXT,
            reason          TEXT,
            is_developer    INTEGER DEFAULT 0,
            from_scanner    INTEGER DEFAULT 0,
            source_resource TEXT,
            admin_name      TEXT,
            admin_discord   TEXT,
            admin_license   TEXT,
            admin_steam     TEXT,
            admin_ip        TEXT,
            admin_server_id INTEGER,
            target_name     TEXT,
            target_discord  TEXT,
            target_license  TEXT,
            target_steam    TEXT,
            target_server_id INTEGER,
            timestamp       TEXT,
            created_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS admin_actions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id       TEXT    UNIQUE,
            command         TEXT,
            command_label   TEXT,
            category        TEXT,
            reason          TEXT,
            is_developer    INTEGER DEFAULT 0,
            from_scanner    INTEGER DEFAULT 0,
            source_resource TEXT,
            admin_name      TEXT,
            admin_discord   TEXT,
            admin_license   TEXT,
            target_name     TEXT,
            target_discord  TEXT,
            target_license  TEXT,
            timestamp       TEXT,
            created_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS server_heartbeats (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            player_count INTEGER,
            max_players  INTEGER,
            players_json TEXT,
            timestamp    TEXT,
            created_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS kill_feed (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            killer_name        TEXT,
            killer_discord     TEXT,
            killer_license     TEXT,
            killer_server_id   INTEGER,
            victim_name        TEXT,
            victim_discord     TEXT,
            victim_license     TEXT,
            victim_server_id   INTEGER,
            weapon             TEXT,
            timestamp          TEXT,
            created_at         TEXT
        );

        CREATE TABLE IF NOT EXISTS disconnect_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            player_name       TEXT,
            player_discord    TEXT,
            player_license    TEXT,
            player_server_id  INTEGER,
            reason            TEXT,
            timestamp         TEXT,
            created_at        TEXT
        );

        CREATE TABLE IF NOT EXISTS raw_webhook_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event      TEXT,
            version    TEXT,
            payload    TEXT,
            created_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_reports_created   ON admin_reports (created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_reports_admin     ON admin_reports (admin_discord);
        CREATE INDEX IF NOT EXISTS idx_reports_category  ON admin_reports (category);
        CREATE INDEX IF NOT EXISTS idx_reports_command   ON admin_reports (command);
        """
    )
    db.commit()
    db.close()
    print("Admin logs tables created.")
