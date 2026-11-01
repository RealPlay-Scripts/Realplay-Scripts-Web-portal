"""
COOLDOWN API — app/api/cooldown_routes.py
=========================================
Routes for the FiveM IP cooldown system.

FiveM server.lua calls:
  POST /api/cooldown/report      — record a new cooldown (no auth, API key only)
  GET  /api/cooldown/check?ip=   — check if an IP is currently cleared by admin

Admin panel / dashboard calls:
  GET  /api/cooldown/logs        — paginated log of all cooldown events
  GET  /api/cooldown/active      — list of IPs currently on cooldown
  POST /api/cooldown/clear/ip    — clear cooldown for an IP address
  POST /api/cooldown/clear/discord — clear all cooldowns for a discord_id

Registration:
  In app/__init__.py add:
    from app.api.cooldown_routes import cooldown_bp
    app.register_blueprint(cooldown_bp, url_prefix='/api/cooldown')
"""

from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from app.models import db, CooldownLog, User
from app.utils import api_key_required, admin_required
from flask_login import current_user, login_required

cooldown_bp = Blueprint('cooldown', __name__)

# ── In-memory cleared-IP set (synced with DB) ────────────────────────────────
# When an admin clears an IP, it's added here so the FiveM script can poll
# /api/cooldown/check and know to lift the block immediately.
# This resets on server restart — that's fine, cleared IPs just expire anyway.
_cleared_ips = set()


# ── FiveM: report a new cooldown being set ──────────────────────────────────

@cooldown_bp.route('/report', methods=['POST'])
@api_key_required('read')
def report_cooldown():
    """
    Called by server.lua when a player is denied and gets cooled down.
    Body JSON:
    {
        "ip":          "ip:1.2.3.4",
        "discord_id":  "123456789",      (optional)
        "player_name": "Ewan.C",         (optional)
        "event":       "cooldown_set",   (or "cooldown_retry")
        "seconds_left": 3600             (optional, for retry events)
    }
    """
    data = request.get_json(silent=True) or {}
    ip = data.get('ip', '').strip()
    if not ip:
        return jsonify({'error': 'ip is required'}), 400

    event = data.get('event', 'cooldown_set')
    if event not in ('cooldown_set', 'cooldown_retry'):
        event = 'cooldown_set'

    log = CooldownLog(
        ip          = ip,
        discord_id  = data.get('discord_id'),
        player_name = data.get('player_name'),
        event       = event,
        seconds_left= data.get('seconds_left'),
    )
    db.session.add(log)
    db.session.commit()

    # Fire Discord webhook log
    _send_cooldown_webhook(log)

    return jsonify({'ok': True, 'id': log.id})


# ── FiveM: check if an admin has cleared this IP ─────────────────────────────

@cooldown_bp.route('/check', methods=['GET'])
@api_key_required('read')
def check_cleared():
    """
    Called by server.lua on each cooldown-blocked join attempt.
    Returns { "cleared": true } if an admin has manually lifted the cooldown.
    server.lua uses this to skip the local cooldown timer.
    GET /api/cooldown/check?ip=ip:1.2.3.4
    """
    ip = request.args.get('ip', '').strip()
    if not ip:
        return jsonify({'error': 'ip is required'}), 400

    cleared = ip in _cleared_ips
    if cleared:
        # Remove from the set after the script has acknowledged it —
        # the cooldown is now lifted, further joins will go through normally.
        _cleared_ips.discard(ip)

    return jsonify({'cleared': cleared})


# ── Admin: list all cooldown log entries ────────────────────────────────────

@cooldown_bp.route('/logs', methods=['GET'])
@login_required
@admin_required
def list_logs():
    """
    GET /api/cooldown/logs?page=1&per_page=50&event=cooldown_set&ip=...
    """
    page     = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 50)), 200)
    event_f  = request.args.get('event')
    ip_f     = request.args.get('ip', '').strip()
    discord_f= request.args.get('discord_id', '').strip()

    q = CooldownLog.query.order_by(CooldownLog.created_at.desc())
    if event_f:
        q = q.filter(CooldownLog.event == event_f)
    if ip_f:
        q = q.filter(CooldownLog.ip.ilike(f'%{ip_f}%'))
    if discord_f:
        q = q.filter(CooldownLog.discord_id == discord_f)

    paginated = q.paginate(page=page, per_page=per_page, error_out=False)
    return jsonify({
        'logs':       [l.to_dict() for l in paginated.items],
        'total':      paginated.total,
        'page':       page,
        'pages':      paginated.pages,
    })


# ── Admin: list currently active (non-expired) cooldowns ────────────────────

@cooldown_bp.route('/active', methods=['GET'])
@login_required
@admin_required
def list_active():
    """
    Returns the most recent cooldown_set event per unique IP that has NOT
    been followed by an admin_cleared event within the cooldown window.
    The FiveM script holds state in memory — this gives the admin panel
    a view of who is currently blocked based on the log records.
    """
    from sqlalchemy import func, and_

    # Subquery: latest cooldown_set per IP
    sub = (
        db.session.query(
            CooldownLog.ip,
            func.max(CooldownLog.created_at).label('last_set')
        )
        .filter(CooldownLog.event == 'cooldown_set')
        .group_by(CooldownLog.ip)
        .subquery()
    )

    # Join back to get full row
    rows = (
        db.session.query(CooldownLog)
        .join(sub, and_(
            CooldownLog.ip == sub.c.ip,
            CooldownLog.created_at == sub.c.last_set,
            CooldownLog.event == 'cooldown_set',
        ))
        .order_by(CooldownLog.created_at.desc())
        .all()
    )

    # Filter: exclude IPs that have a more-recent admin_cleared entry
    result = []
    for row in rows:
        cleared = CooldownLog.query.filter(
            CooldownLog.ip == row.ip,
            CooldownLog.event == 'admin_cleared',
            CooldownLog.created_at > row.created_at,
        ).first()
        if not cleared:
            result.append(row.to_dict())

    return jsonify({'active': result})


# ── Admin: clear cooldown by IP ─────────────────────────────────────────────

@cooldown_bp.route('/clear/ip', methods=['POST'])
@login_required
@admin_required
def clear_by_ip():
    """
    Body: { "ip": "ip:1.2.3.4", "note": "Verified on Discord" }
    Adds the IP to _cleared_ips so the next /check call from server.lua
    lifts the block, and logs the action.
    """
    data = request.get_json(silent=True) or {}
    ip   = data.get('ip', '').strip()
    if not ip:
        return jsonify({'error': 'ip is required'}), 400

    _cleared_ips.add(ip)

    log = CooldownLog(
        ip         = ip,
        event      = 'admin_cleared',
        cleared_by = current_user.id,
        note       = data.get('note', ''),
    )
    db.session.add(log)
    db.session.commit()

    current_app.logger.info(
        f'[Cooldown] Admin {current_user.username} cleared IP {ip}'
    )
    return jsonify({'ok': True, 'cleared_ip': ip})


# ── Admin: clear all cooldowns for a Discord ID ──────────────────────────────

@cooldown_bp.route('/clear/discord', methods=['POST'])
@login_required
@admin_required
def clear_by_discord():
    """
    Body: { "discord_id": "123456789", "note": "Whitelisted manually" }
    Finds all recent cooldown_set logs for this Discord ID and clears every IP.
    """
    data       = request.get_json(silent=True) or {}
    discord_id = data.get('discord_id', '').strip()
    if not discord_id:
        return jsonify({'error': 'discord_id is required'}), 400

    # Find all IPs this discord_id has been cooled down on
    rows = (
        CooldownLog.query
        .filter(CooldownLog.discord_id == discord_id,
                CooldownLog.event == 'cooldown_set')
        .all()
    )
    ips_cleared = set()
    for row in rows:
        _cleared_ips.add(row.ip)
        ips_cleared.add(row.ip)
        log = CooldownLog(
            ip         = row.ip,
            discord_id = discord_id,
            event      = 'admin_cleared',
            cleared_by = current_user.id,
            note       = data.get('note', f'Bulk clear by discord_id'),
        )
        db.session.add(log)

    db.session.commit()

    current_app.logger.info(
        f'[Cooldown] Admin {current_user.username} cleared discord_id {discord_id} '
        f'({len(ips_cleared)} IPs)'
    )
    return jsonify({'ok': True, 'cleared_ips': list(ips_cleared)})


# ── Discord webhook logger ───────────────────────────────────────────────────

def _send_cooldown_webhook(log: CooldownLog):
    """Posts a cooldown event embed to the whitelist log webhook."""
    import threading
    import requests as _req

    webhook_url = current_app.config.get(
        'WHITELIST_LOG_WEBHOOK_URL',
        'https://discord.com/api/webhooks/1503520743072202954/jihTq1jKAUYT-b2yo4sHCmtI9KwHEIZSoTW00AtromkmV_P_2KmPfshsz5rvYShHShru'
    )
    if not webhook_url:
        return

    event_meta = {
        'cooldown_set':   ('🔒 Cooldown Set',   0xED4245),
        'cooldown_retry': ('🔁 Retry Blocked',   0xFEE75C),
        'admin_cleared':  ('✅ Cooldown Cleared', 0x57F287),
    }
    title, colour = event_meta.get(log.event, ('📋 Cooldown Event', 0x99AAB5))

    fields = [
        {'name': '🌐 IP',          'value': f'`{log.ip}`',         'inline': True},
        {'name': '🪪 Discord ID',  'value': f'`{log.discord_id or "unknown"}`', 'inline': True},
        {'name': '👤 Player',      'value': log.player_name or 'unknown', 'inline': True},
    ]
    if log.seconds_left:
        mins = max(1, (log.seconds_left + 59) // 60)
        fields.append({'name': '⏳ Time Left', 'value': f'{mins} min', 'inline': True})

    embed = {
        'title':     title,
        'color':     colour,
        'fields':    fields,
        'footer':    {'text': 'CFRP Cooldown System'},
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        'username':   'Cooldown Logger',
        'avatar_url': 'https://cdn.discordapp.com/embed/avatars/0.png',
        'embeds':     [embed],
    }

    def _post():
        try:
            resp = _req.post(webhook_url, json=payload, timeout=8)
            resp.raise_for_status()
        except Exception:
            pass

    threading.Thread(target=_post, daemon=True).start()
