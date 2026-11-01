"""
WHITELIST INTEGRATION — api/whitelist_routes.py
================================================
Add these routes to your app/api/routes.py  (or register this file as a
separate blueprint and add it to app/__init__.py).

Public routes (called by FiveM server.lua every player join):
  GET  /api/whitelist/status          → { "enabled": true|false }
  GET  /api/whitelist/check?discord_id=<id>
                                      → { "allowed": true|false, "reason": "...", "message": "..." }
                                        Checks: (1) player has a linked website account,
                                                (2) player has role 1324156330096721980 in Discord.

Admin routes (require X-API-Key with admin scope OR logged-in admin):
  POST /api/whitelist/toggle          → toggle on/off
  POST /api/whitelist/set             → set explicitly { "enabled": true|false }
  GET  /api/whitelist/schedules       → list schedules
  POST /api/whitelist/schedules       → create schedule
  DELETE /api/whitelist/schedules/<id> → delete schedule
  POST /api/whitelist/schedules/tick  → (called by cron/beat) fire due schedules

Discord logging:
  Every join attempt (allowed or denied) is posted to WHITELIST_LOG_WEBHOOK_URL
  as a rich embed so staff can monitor access in real time.
"""

import threading
from datetime import datetime, timezone, timedelta
from flask import request, jsonify, current_app
from flask_login import current_user, login_required
from app.api import api_bp                       # existing blueprint
from app.models import db, SiteSettings, AuditLog
from app.utils import api_key_required

# ---------------------------------------------------------------------------
# Import WhitelistSchedule — add after pasting WhitelistSchedule into models.py
# ---------------------------------------------------------------------------
from app.models import WhitelistSchedule         # noqa: E402  (add to existing import line)


# ── Config ────────────────────────────────────────────────────────────────

# Discord role ID that players must have to be allowed into the server.
# This role must be present on the player's Discord account in the guild.
REQUIRED_DISCORD_ROLE_ID = '1503515226375716917'

# Webhook URL for the join/deny log channel.
# Override via WHITELIST_LOG_WEBHOOK_URL in your .env / app config.
_DEFAULT_WEBHOOK = 'https://discord.com/api/webhooks/1503520743072202954/jihTq1jKAUYT-b2yo4sHCmtI9KwHEIZSoTW00AtromkmV_P_2KmPfshsz5rvYShHShru'


# ── Discord webhook helper ─────────────────────────────────────────────────

def _send_whitelist_log(
    *,
    discord_id: str,
    allowed: bool,
    reason: str,
    username: str | None = None,
    avatar_url: str | None = None,
) -> None:
    """
    Fire-and-forget: post a rich embed to the configured Discord webhook.
    Runs in a daemon thread so it never blocks the FiveM join response.

    Parameters
    ----------
    discord_id  : The player's Discord snowflake ID.
    allowed     : True = joined successfully, False = denied.
    reason      : Machine-readable reason code from whitelist_check.
    username    : Display name pulled from the linked User row (optional).
    avatar_url  : Discord CDN avatar URL (optional).
    """
    import requests as _req  # local import — avoids circular at module level

    webhook_url = current_app.config.get('WHITELIST_LOG_WEBHOOK_URL', _DEFAULT_WEBHOOK)
    if not webhook_url:
        return

    # ── Embed colours & icons ─────────────────────────────────────────────
    if allowed:
        colour     = 0x57F287   # Discord green
        status_txt = '✅  Joined'
        title      = 'Player Joined'
    else:
        colour     = 0xED4245   # Discord red
        status_txt = '⛔  Denied'
        title      = 'Join Denied'

    # Human-readable reason labels
    reason_labels = {
        'whitelist_disabled':  'Whitelist is OFF (open access)',
        'discord_id_required': 'No Discord ID supplied',
        'not_registered':      'Not registered on the website',
        'missing_role':        'Missing whitelisted role',
        'discord_api_error':   'Discord API error — could not verify roles',
    }
    reason_label = reason_labels.get(reason, reason)

    # ── Build embed fields ────────────────────────────────────────────────
    now_ts = int(datetime.now(timezone.utc).timestamp())

    fields = [
        {
            'name':   '🪪  Discord ID',
            'value':  f'`{discord_id}`\n<@{discord_id}>',
            'inline': True,
        },
        {
            'name':   '📋  Status',
            'value':  status_txt,
            'inline': True,
        },
        {
            'name':   '📌  Reason',
            'value':  reason_label,
            'inline': False,
        },
    ]

    if username:
        fields.insert(0, {
            'name':   '👤  Username',
            'value':  f'`{username}`',
            'inline': True,
        })

    embed = {
        'title':       title,
        'color':       colour,
        'fields':      fields,
        'footer':      {'text': 'GSRP Whitelist System'},
        'timestamp':   datetime.now(timezone.utc).isoformat(),
    }

    if avatar_url:
        embed['thumbnail'] = {'url': avatar_url}

    payload = {
        'username':   'Whitelist Logger',
        'avatar_url': 'https://i.postimg.cc/BSfZm7cw/IMG-5541.jpg',
        'embeds':     [embed],
    }

    def _post():
        try:
            resp = _req.post(webhook_url, json=payload, timeout=8)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            # Log but never crash the request thread
            try:
                current_app.logger.warning(f'Whitelist webhook failed: {exc}')
            except RuntimeError:
                pass  # outside app context — swallow silently

    t = threading.Thread(target=_post, daemon=True)
    t.start()


# ── helpers ────────────────────────────────────────────────────────────────

def _whitelist_enabled() -> bool:
    return SiteSettings.get('whitelist_enabled', True)


def _set_whitelist(enabled: bool, actor_id=None, reason='api'):
    SiteSettings.set('whitelist_enabled', enabled, 'bool',
                     'FiveM whitelist active', 'whitelist')
    # Audit log
    log = AuditLog(
        user_id=actor_id,
        action='whitelist.set_' + ('on' if enabled else 'off'),
        resource_type='setting',
        resource_id='whitelist_enabled',
    )
    db.session.add(log)
    db.session.commit()

# ── Public: FiveM checks this on every player connection ──────────────────

@api_bp.route('/whitelist/status')
def whitelist_status():
    """
    No auth required — called by the FiveM resource on every playerConnecting.
    Returns:
      { "enabled": true }   → whitelist is active (normal behaviour)
      { "enabled": false }  → whitelist is OFF (let everyone in / custom msg)
    """
    return jsonify({'enabled': _whitelist_enabled()})


@api_bp.route('/whitelist/check')
def whitelist_check():
    """
    Per-player join check — called by the FiveM resource on every playerConnecting.
    Pass the player's Discord ID as a query parameter:
      GET /api/whitelist/check?discord_id=<id>

    Two requirements must both be satisfied for the player to be allowed in:
      1. The player must have a website account with that Discord ID linked
         (i.e. they have gone through the Discord OAuth connection flow).
      2. The player must have role ID 1324156330096721980 on Discord in the guild.

    Returns JSON:
      { "allowed": true }
      { "allowed": false, "reason": "not_registered" }
      { "allowed": false, "reason": "missing_role" }
      { "allowed": false, "reason": "discord_id_required" }
      { "allowed": false, "reason": "discord_api_error" }

    Every outcome (allow or deny) is logged to the Discord webhook channel.

    If the global whitelist is disabled this endpoint still returns allowed=true
    so that the FiveM server.lua can simply call /status first and skip the
    per-player check when the whitelist is off.
    """
    # If whitelist is globally off, everyone is allowed — skip per-player checks.
    if not _whitelist_enabled():
        # Still log so staff can see open-access joins
        discord_id = request.args.get('discord_id', '').strip()
        _send_whitelist_log(
            discord_id=discord_id or 'unknown',
            allowed=True,
            reason='whitelist_disabled',
        )
        return jsonify({'allowed': True, 'reason': 'whitelist_disabled'})

    discord_id = request.args.get('discord_id', '').strip()
    if not discord_id:
        _send_whitelist_log(
            discord_id='unknown',
            allowed=False,
            reason='discord_id_required',
        )
        return jsonify({'allowed': False, 'reason': 'discord_id_required'})

    # ── Requirement 1: player must be connected to the website ────────────
    from app.models import User
    user = User.query.filter_by(discord_id=discord_id, discord_verified=True).first()
    if not user:
        _send_whitelist_log(
            discord_id=discord_id,
            allowed=False,
            reason='not_registered',
        )
        return jsonify({
            'allowed': False,
            'reason': 'not_registered',
            'message': SiteSettings.get(
                'whitelist_kick_msg',
                'You are not whitelisted on this server!\n\nTo join, please visit our Discord: https://discord.gg/VpWAtzPw9Z and apply.\n\nYou must link your Discord account on our website:\n%s\n\nOnce linked, restart FiveM and try again.'
            ),
        })

    # Resolve display info for the webhook embed (best-effort)
    _username   = user.username if user else None
    _avatar_url = user.discord_avatar_url if user else None

    # ── Requirement 2: player must have the required Discord role ─────────
    from app.utils import get_discord_member
    guild_id = current_app.config.get('DISCORD_GUILD_ID', '')

    member = get_discord_member(discord_id, guild_id)
    if member is None:
        # Could not reach Discord API — fail closed (deny) to be safe.
        current_app.logger.warning(
            f'whitelist/check: Discord API error for discord_id={discord_id}'
        )
        _send_whitelist_log(
            discord_id=discord_id,
            allowed=False,
            reason='discord_api_error',
            username=_username,
            avatar_url=_avatar_url,
        )
        return jsonify({
            'allowed': False,
            'reason': 'discord_api_error',
            'message': 'Could not verify your Discord roles. Please try again.',
        })

    member_roles = member.get('roles', [])
    if REQUIRED_DISCORD_ROLE_ID not in member_roles:
        _send_whitelist_log(
            discord_id=discord_id,
            allowed=False,
            reason='missing_role',
            username=_username,
            avatar_url=_avatar_url,
        )
        return jsonify({
            'allowed': False,
            'reason': 'missing_role',
            'message': SiteSettings.get(
                'whitelist_kick_msg',
                'You are not whitelisted on this server!\n\nTo join, please visit our Discord: https://discord.gg/VpWAtzPw9Z and apply.\n\nYou must link your Discord account on our website:\n%s\n\nOnce linked, restart FiveM and try again.'
            ),
        })

    # Both checks passed — player is allowed.
    _send_whitelist_log(
        discord_id=discord_id,
        allowed=True,
        reason='allowed',
        username=_username,
        avatar_url=_avatar_url,
    )
    return jsonify({'allowed': True})


# ── Admin: toggle / set ───────────────────────────────────────────────────

@api_bp.route('/whitelist/toggle', methods=['POST'])
@api_key_required('admin')
def whitelist_toggle():
    current = _whitelist_enabled()
    _set_whitelist(not current, reason='api_toggle')
    return jsonify({'enabled': not current})


@api_bp.route('/whitelist/set', methods=['POST'])
@api_key_required('admin')
def whitelist_set():
    data = request.get_json(silent=True) or {}
    if 'enabled' not in data:
        return jsonify({'error': 'Missing "enabled" field'}), 400
    enabled = bool(data['enabled'])
    _set_whitelist(enabled, reason='api_set')
    return jsonify({'enabled': enabled})


# ── Admin: schedules ──────────────────────────────────────────────────────

@api_bp.route('/whitelist/schedules', methods=['GET'])
@api_key_required('admin')
def list_schedules():
    schedules = WhitelistSchedule.query.order_by(
        WhitelistSchedule.scheduled_at.asc()
    ).all()
    return jsonify({'schedules': [s.to_dict() for s in schedules]})


@api_bp.route('/whitelist/schedules', methods=['POST'])
@api_key_required('admin')
def create_schedule():
    """
    Body:
    {
      "label":        "Open for weekend",
      "enabled":      true,
      "scheduled_at": "2026-04-20T18:00:00",   ← UTC ISO-8601
      "repeat_type":  "once"                    ← once | daily | weekly
    }
    """
    data = request.get_json(silent=True) or {}
    required = ('enabled', 'scheduled_at')
    for f in required:
        if f not in data:
            return jsonify({'error': f'Missing field: {f}'}), 400

    try:
        dt = datetime.fromisoformat(data['scheduled_at'].replace('Z', ''))
    except ValueError:
        return jsonify({'error': 'Invalid scheduled_at format (use ISO-8601)'}), 400

    repeat = data.get('repeat_type', 'once')
    if repeat not in ('once', 'daily', 'weekly'):
        return jsonify({'error': 'repeat_type must be once | daily | weekly'}), 400

    schedule = WhitelistSchedule(
        label        = data.get('label', ''),
        enabled      = bool(data['enabled']),
        scheduled_at = dt,
        repeat_type  = repeat,
        created_by   = current_user.id if current_user.is_authenticated else None,
    )
    db.session.add(schedule)
    db.session.commit()
    return jsonify(schedule.to_dict()), 201


@api_bp.route('/whitelist/schedules/<int:schedule_id>', methods=['DELETE'])
@api_key_required('admin')
def delete_schedule(schedule_id):
    schedule = WhitelistSchedule.query.get_or_404(schedule_id)
    db.session.delete(schedule)
    db.session.commit()
    return jsonify({'deleted': schedule_id})


# ── Scheduler tick (call this from APScheduler / cron every minute) ────────

@api_bp.route('/whitelist/schedules/tick', methods=['POST'])
@api_key_required('admin')
def tick_schedules():
    """
    Fires all due schedules.
    Call this endpoint every minute from a cron job or APScheduler:
      curl -s -X POST https://web.cfrp.co.za/api/whitelist/schedules/tick \
           -H "X-API-Key: YOUR_KEY"

    Or from APScheduler in your app/__init__.py:
      from apscheduler.schedulers.background import BackgroundScheduler
      scheduler = BackgroundScheduler()

      def tick():
          with app.app_context():
              from app.api.whitelist_routes import _tick
              _tick()

      scheduler.add_job(tick, 'interval', minutes=1)
      scheduler.start()
    """
    fired = _tick()
    return jsonify({'fired': fired})


def _tick():
    """Internal tick — call from scheduler directly if preferred."""
    now = datetime.now(timezone.utc)
    due = WhitelistSchedule.query.filter(
        WhitelistSchedule.scheduled_at <= now,
        WhitelistSchedule.is_executed == False,   # noqa: E712
    ).all()

    fired = []
    for s in due:
        _set_whitelist(s.enabled, actor_id=s.created_by, reason=f'schedule:{s.id}')
        fired.append(s.id)

        if s.repeat_type == 'once':
            s.is_executed = True
        elif s.repeat_type == 'daily':
            s.scheduled_at = s.scheduled_at + timedelta(days=1)
        elif s.repeat_type == 'weekly':
            s.scheduled_at = s.scheduled_at + timedelta(weeks=1)

    db.session.commit()
    return fired
