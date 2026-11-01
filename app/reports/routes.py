import requests
from datetime import datetime, timezone
from flask import (render_template, redirect, url_for, flash, request,
                   jsonify, current_app, abort)
from flask_login import login_required, current_user

from app.reports import reports_bp
from app.models import db, Report, ReportMessage, ReportSuspectMessage, Notification, AuditLog, User, Permission, user_roles

# ─────────────────────────────────────────────────────────────────────────────
# Discord DM helpers (uses the bot token to DM users directly)
# ─────────────────────────────────────────────────────────────────────────────

DISCORD_API = "https://discord.com/api/v10"


def _bot_headers() -> dict:
    """Auth headers using the bot token stored in app config."""
    token = current_app.config.get("DISCORD_BOT_TOKEN", "")
    return {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }


def _open_dm_channel(discord_id: str) -> str | None:
    """Create (or fetch existing) DM channel with a user. Returns channel_id or None."""
    try:
        r = requests.post(
            f"{DISCORD_API}/users/@me/channels",
            headers=_bot_headers(),
            json={"recipient_id": discord_id},
            timeout=8,
        )
        if r.status_code in (200, 201):
            return r.json().get("id")
        current_app.logger.warning(f"DM channel open failed ({r.status_code}): {r.text[:200]}")
    except Exception as exc:
        current_app.logger.error(f"DM channel open error: {exc}")
    return None


def _send_dm(discord_id: str, embed: dict, content: str = "") -> bool:
    """Send a DM embed to a Discord user. Returns True on success."""
    if not discord_id:
        return False
    channel_id = _open_dm_channel(discord_id)
    if not channel_id:
        return False
    payload = {"embeds": [embed]}
    if content:
        payload["content"] = content
    try:
        r = requests.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers=_bot_headers(),
            json=payload,
            timeout=8,
        )
        if r.status_code in (200, 201):
            return True
        current_app.logger.warning(f"DM send failed ({r.status_code}): {r.text[:200]}")
    except Exception as exc:
        current_app.logger.error(f"DM send error: {exc}")
    return False


def _dm_report_received(report: Report):
    """DM the reporter confirming their report was received."""
    reporter = report.reporter
    if not reporter or not reporter.discord_id:
        return

    meta     = _TYPE_META.get(report.report_type, _TYPE_META["bug"])
    site     = _site_url()
    view_url = f"{site}/reports/{report.id}"

    type_icon = "🎮" if report.report_type == "player" else "🐛"

    # Truncate description for preview
    desc_preview = (report.description[:120] + "…") if len(report.description) > 120 else report.description

    embed = {
        "author": {
            "name":     "Cape Flats Roleplay — Report System",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png",
        },
        "title": f"✅  Report Successfully Submitted",
        "description": (
            f"Hey **{reporter.username}**, your report has been received and logged.\n"
            f"Our staff team will review it as soon as possible. "
            f"You will be notified here when there is an update."
        ),
        "color": 0x57F287,   # Discord green
        "fields": [
            {
                "name":  "📋  Case Reference",
                "value": f"```\nCase #{report.id}\n```",
                "inline": True,
            },
            {
                "name":  f"{type_icon}  Report Type",
                "value": f"```\n{meta['label']}\n```",
                "inline": True,
            },
            {
                "name":  "🔔  Current Status",
                "value": "```\nPending Review\n```",
                "inline": True,
            },
            {
                "name":  "📝  Your Report",
                "value": f"**{report.title}**\n{desc_preview}",
                "inline": False,
            },
            {
                "name":  "🔗  Track Your Report",
                "value": (
                    f"[**View Report Status →**]({view_url})\n"
                    f"-# You can reply to staff and follow updates on the portal."
                ),
                "inline": False,
            },
        ],
        "footer": {
            "text":     f"Cape Flats Roleplay  •  Case #{report.id}  •  Reports Portal",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _send_dm(
        reporter.discord_id,
        embed,
        content=f"📬  **Your report has been submitted**, {reporter.username}! Here's your confirmation:",
    )


def _dm_staff_reply(report: Report, staff_name: str, message_content: str):
    """DM the reporter when a staff member sends a Private Communication."""
    reporter = report.reporter
    if not reporter or not reporter.discord_id:
        return

    meta     = _TYPE_META.get(report.report_type, _TYPE_META["bug"])
    site     = _site_url()
    view_url = f"{site}/reports/{report.id}"

    status_label = Report.STATUS_LABELS.get(report.status, report.status.title())
    status_emoji = _STATUS_EMOJI.get(report.status, "⚪")
    type_icon    = "🎮" if report.report_type == "player" else "🐛"

    embed = {
        "author": {
            "name":     f"Message from {staff_name}  •  CFRP Staff",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        },
        "title": "💬  You Have a New Message",
        "description": (
            f"> {message_content[:1800]}"
        ),
        "color": 0x5865F2,   # Discord blurple
        "fields": [
            {
                "name":  "📋  Case Reference",
                "value": f"```\nCase #{report.id}\n```",
                "inline": True,
            },
            {
                "name":  f"{type_icon}  Report Type",
                "value": f"```\n{meta['label']}\n```",
                "inline": True,
            },
            {
                "name":  "🔔  Status",
                "value": f"```\n{status_emoji} {status_label}\n```",
                "inline": True,
            },
            {
                "name":  "📌  Your Report",
                "value": f"**{report.title}**",
                "inline": False,
            },
            {
                "name":  "↩️  Reply to Staff",
                "value": (
                    f"[**Open Report to Reply →**]({view_url})\n"
                    f"-# Head to the portal to respond directly to this message."
                ),
                "inline": False,
            },
        ],
        "footer": {
            "text":     f"Cape Flats Roleplay  •  Case #{report.id}  •  Staff Communication",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _send_dm(
        reporter.discord_id,
        embed,
        content=f"<@{reporter.discord_id}> — **{staff_name}** has sent you a message regarding **Case #{report.id}**.",
    )


def _dm_status_update(report: Report, old_status: str, actor_name: str):
    """DM the reporter when their report status changes (non-resolved)."""
    reporter = report.reporter
    if not reporter or not reporter.discord_id:
        return

    meta      = _TYPE_META.get(report.report_type, _TYPE_META["bug"])
    site      = _site_url()
    view_url  = f"{site}/reports/{report.id}"
    type_icon = "🎮" if report.report_type == "player" else "🐛"

    old_label = Report.STATUS_LABELS.get(old_status, old_status.title())
    new_label = Report.STATUS_LABELS.get(report.status, report.status.title())
    old_emoji = _STATUS_EMOJI.get(old_status, "⚪")
    new_emoji = _STATUS_EMOJI.get(report.status, "⚪")

    # Pick colour based on new status
    color_map = {
        "seen":        0x6366F1,
        "in_progress": 0x3B82F6,
        "waiting":     0xF97316,
    }
    color = color_map.get(report.status, 0x6B7280)

    embed = {
        "author": {
            "name":     f"Status Update by {actor_name}  •  CFRP Staff",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        },
        "title": "🔄  Your Report Status Has Changed",
        "description": (
            f"Staff have reviewed your case and updated its status.\n\n"
            f"{old_emoji}  ~~{old_label}~~  **→**  {new_emoji}  **{new_label}**"
        ),
        "color": color,
        "fields": [
            {
                "name":  "📋  Case Reference",
                "value": f"```\nCase #{report.id}\n```",
                "inline": True,
            },
            {
                "name":  f"{type_icon}  Report Type",
                "value": f"```\n{meta['label']}\n```",
                "inline": True,
            },
            {
                "name":  "🔔  New Status",
                "value": f"```\n{new_emoji} {new_label}\n```",
                "inline": True,
            },
            {
                "name":  "📌  Your Report",
                "value": f"**{report.title}**",
                "inline": False,
            },
            {
                "name":  "🔗  View Report",
                "value": (
                    f"[**Open Your Report →**]({view_url})\n"
                    f"-# Check the portal for any messages from staff."
                ),
                "inline": False,
            },
        ],
        "footer": {
            "text":     f"Cape Flats Roleplay  •  Case #{report.id}  •  Reports Portal",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _send_dm(
        reporter.discord_id,
        embed,
        content=f"<@{reporter.discord_id}> — The status of **Case #{report.id}** has been updated.",
    )


def _dm_resolved(report: Report, actor_name: str):
    """DM the reporter when their report is marked resolved."""
    reporter = report.reporter
    if not reporter or not reporter.discord_id:
        return

    meta      = _TYPE_META.get(report.report_type, _TYPE_META["bug"])
    site      = _site_url()
    view_url  = f"{site}/reports/{report.id}"
    type_icon = "🎮" if report.report_type == "player" else "🐛"
    staff_note = report.staff_note or "No closing note provided."

    # Calculate time open
    if report.resolved_at and report.created_at:
        created = report.created_at.replace(tzinfo=timezone.utc) if report.created_at.tzinfo is None else report.created_at
        resolved = report.resolved_at.replace(tzinfo=timezone.utc) if report.resolved_at.tzinfo is None else report.resolved_at
        delta   = resolved - created
        hours   = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        duration = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    else:
        duration = "—"

    embed = {
        "author": {
            "name":     f"Resolved by {actor_name}  •  CFRP Staff",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        },
        "title": "✅  Your Report Has Been Resolved",
        "description": (
            f"**Case #{report.id}** has been reviewed and closed by staff.\n"
            f"Thank you for taking the time to report this — your feedback helps keep CFRP running smoothly."
        ),
        "color": 0x57F287,   # Discord green
        "fields": [
            {
                "name":  "📋  Case Reference",
                "value": f"```\nCase #{report.id}\n```",
                "inline": True,
            },
            {
                "name":  f"{type_icon}  Report Type",
                "value": f"```\n{meta['label']}\n```",
                "inline": True,
            },
            {
                "name":  "⏱️  Time to Resolve",
                "value": f"```\n{duration}\n```",
                "inline": True,
            },
            {
                "name":  "📌  Your Report",
                "value": f"**{report.title}**",
                "inline": False,
            },
            {
                "name":  "📝  Staff Closing Note",
                "value": f"> {staff_note[:500]}",
                "inline": False,
            },
            {
                "name":  "🔗  View Closed Case",
                "value": (
                    f"[**View Full Report →**]({view_url})\n"
                    f"-# If you believe this was closed in error, please open a new report."
                ),
                "inline": False,
            },
        ],
        "footer": {
            "text":     f"Cape Flats Roleplay  •  Case #{report.id}  •  Reports Portal",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _send_dm(
        reporter.discord_id,
        embed,
        content=f"<@{reporter.discord_id}> — **Case #{report.id}** has been resolved. Here's your summary:",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Reported Player DM helpers
# ─────────────────────────────────────────────────────────────────────────────

def _dm_reported_player_notice(report: Report):
    """DM the reported user when a player report is filed against them."""
    suspect = report.reported_user
    if not suspect or not suspect.discord_id:
        return

    site     = _site_url()

    embed = {
        "author": {
            "name":     "Cape Flats Roleplay — Staff Notice",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        },
        "title": "⚠️  Notice: Player Report Filed",
        "description": (
            f"Hey **{suspect.username}**, this is an automated notice from the CFRP staff team.\n\n"
            "A player report has been filed that involves you. **This does not mean any action has been taken.** "
            "Our staff are currently reviewing the report and will be in touch if they need more information from you."
        ),
        "color": 0xF59E0B,   # amber
        "fields": [
            {
                "name":  "🔍  What Happens Next",
                "value": (
                    "Staff will review all available evidence.\n"
                    "You may receive a follow-up message here if staff wish to hear your side.\n"
                    "A final decision will be communicated to you once the investigation is complete."
                ),
                "inline": False,
            },
            {
                "name":  "📋  Case Reference",
                "value": f"```\nCase #{report.id}\n```",
                "inline": True,
            },
            {
                "name":  "🔔  Current Status",
                "value": "```\n🟡 Under Investigation\n```",
                "inline": True,
            },
            {
                "name":  "ℹ️  Questions?",
                "value": f"If you have questions, please wait for staff to contact you via this DM.",
                "inline": False,
            },
        ],
        "footer": {
            "text":     f"Cape Flats Roleplay  •  Case #{report.id}  •  Staff Investigation",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _send_dm(
        suspect.discord_id,
        embed,
        content=f"<@{suspect.discord_id}> — You have a notice from the CFRP staff team.",
    )


def _dm_reported_player_staff_message(report: Report, staff_name: str, message_content: str):
    """DM the reported user when a staff member sends them a message."""
    suspect = report.reported_user
    if not suspect or not suspect.discord_id:
        return

    embed = {
        "author": {
            "name":     f"Message from {staff_name}  •  CFRP Staff",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        },
        "title": "💬  Message from Staff Regarding Your Case",
        "description": f"> {message_content[:1800]}",
        "color": 0xF97316,   # orange — distinct from reporter purple
        "fields": [
            {
                "name":  "📋  Case Reference",
                "value": f"```\nCase #{report.id}\n```",
                "inline": True,
            },
            {
                "name":  "🔔  Status",
                "value": f"```\n{_STATUS_EMOJI.get(report.status,'⚪')} {Report.STATUS_LABELS.get(report.status, report.status.title())}\n```",
                "inline": True,
            },
            {
                "name":  "ℹ️  Note",
                "value": "This message is part of a staff investigation. Please respond honestly and promptly.",
                "inline": False,
            },
        ],
        "footer": {
            "text":     f"Cape Flats Roleplay  •  Case #{report.id}  •  Staff Communication",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _send_dm(
        suspect.discord_id,
        embed,
        content=f"<@{suspect.discord_id}> — **{staff_name}** has a message for you regarding **Case #{report.id}**.",
    )


def _dm_reported_player_resolved(report: Report):
    """DM the reported user when the case is resolved."""
    suspect = report.reported_user
    if not suspect or not suspect.discord_id:
        return

    staff_note = report.staff_note or "The investigation has been concluded."

    embed = {
        "author": {
            "name":     "Cape Flats Roleplay — Case Closed",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        },
        "title": "✅  Investigation Concluded — Case Closed",
        "description": (
            f"Hey **{suspect.username}**, the staff investigation regarding **Case #{report.id}** has been concluded.\n\n"
            "No further action is required from you at this time."
        ),
        "color": 0x57F287,
        "fields": [
            {
                "name":  "📝  Staff Note",
                "value": f"> {staff_note[:500]}",
                "inline": False,
            },
            {
                "name":  "📋  Case Reference",
                "value": f"```\nCase #{report.id}\n```",
                "inline": True,
            },
            {
                "name":  "🔔  Outcome",
                "value": "```\n🟢 Resolved\n```",
                "inline": True,
            },
        ],
        "footer": {
            "text":     f"Cape Flats Roleplay  •  Case #{report.id}  •  Staff Investigation",
            "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _send_dm(
        suspect.discord_id,
        embed,
        content=f"<@{suspect.discord_id}> — **Case #{report.id}** has been concluded.",
    )


def _get_staff_users():
    """Return all users who have the admin.access permission via any role."""
    perm = Permission.query.filter_by(name='admin.access').first()
    if not perm:
        return []
    role_ids = [r.id for r in perm.roles]
    if not role_ids:
        return []
    rows = db.session.execute(
        db.select(user_roles.c.user_id).where(user_roles.c.role_id.in_(role_ids))
    ).fetchall()
    user_ids = list({r[0] for r in rows})
    return User.query.filter(User.id.in_(user_ids)).all() if user_ids else []


REPORTS_WEBHOOK = (
    'https://discord.com/api/webhooks/1493646232676204614/'
    'iP1D9C3lorwm68_pLDeT78MHgvZJENjHF7OVihd9Z-pdYyVsYD_MOSFHMpODIcPwlIIi'
)

# ── Status colours used in Discord embeds ──────────────────────────────────────
_EMBED_COLORS = {
    'pending':     0xF59E0B,
    'seen':        0x6366F1,
    'in_progress': 0x3B82F6,
    'waiting':     0xF97316,
    'resolved':    0x22C55E,
}


def _require_staff(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.has_permission('admin.access'):
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────────────────────
# Discord helpers
# ─────────────────────────────────────────────────────────────────────────────

def _post_webhook(payload: dict):
    """Fire-and-forget POST to the reports Discord webhook."""
    try:
        requests.post(REPORTS_WEBHOOK, json=payload, timeout=8)
    except Exception as exc:
        current_app.logger.error(f'Reports webhook error: {exc}')


def _site_url():
    return current_app.config.get('SITE_URL', '').rstrip('/')


def _admin_report_url(report_id: int) -> str:
    return f"{_site_url()}/reports/admin/{report_id}"


def _user_report_url(report_id: int) -> str:
    return f"{_site_url()}/reports/{report_id}"


def _reporter_avatar(report: Report) -> str | None:
    """Return Discord CDN avatar URL for the reporter, or None."""
    u = report.reporter
    if u and u.discord_id and u.discord_avatar:
        return f"https://cdn.discordapp.com/avatars/{u.discord_id}/{u.discord_avatar}.png?size=64"
    return None


def _actor_avatar(actor: User) -> str | None:
    if actor and actor.discord_id and actor.discord_avatar:
        return f"https://cdn.discordapp.com/avatars/{actor.discord_id}/{actor.discord_avatar}.png?size=64"
    return None


def _divider() -> dict:
    """Blank separator field (full-width invisible spacer)."""
    return {'name': '\u200b', 'value': '\u200b', 'inline': False}


# ── Status display strings ─────────────────────────────────────────────────────
_STATUS_EMOJI = {
    'pending':     '🟡',
    'seen':        '🔵',
    'in_progress': '🔷',
    'waiting':     '🟠',
    'resolved':    '🟢',
}

_TYPE_META = {
    'player': {'emoji': '🎮', 'label': 'Player Report', 'thumbnail': 'https://i.imgur.com/4M34hi2.png'},
    'bug':    {'emoji': '🐛', 'label': 'Bug Report',    'thumbnail': 'https://i.imgur.com/OApCrGr.png'},
}


def _send_new_report_webhook(report: Report):
    site      = _site_url()
    case_url  = _admin_report_url(report.id)
    meta      = _TYPE_META.get(report.report_type, _TYPE_META['bug'])
    color     = _EMBED_COLORS.get(report.status, 0x6B7280)
    reporter  = report.reporter
    avatar    = _reporter_avatar(report)

    # Truncate description safely
    desc_preview = report.description[:300] + ('…' if len(report.description) > 300 else '')

    # Core identity fields
    fields = [
        {'name': '📋  Case ID',   'value': f'`#{report.id}`',                                       'inline': True},
        {'name': '📁  Type',      'value': f"{meta['emoji']}  {meta['label']}",                     'inline': True},
        {'name': '🔔  Status',    'value': f"{_STATUS_EMOJI['pending']}  Pending",                  'inline': True},
    ]

    # Type-specific fields
    if report.report_type == 'player':
        target = report.reported_player or '_Not provided_'
        fields.append({'name': '🎯  Reported Player', 'value': target, 'inline': True})
        if report.server_id:
            fields.append({'name': '🖥️  Server / Session', 'value': f'`{report.server_id}`', 'inline': True})
        # pad to even row
        if len(fields) % 3 != 0:
            fields.append({'name': '\u200b', 'value': '\u200b', 'inline': True})

    elif report.report_type == 'bug':
        if report.steps_to_reproduce:
            steps = report.steps_to_reproduce[:200] + ('…' if len(report.steps_to_reproduce) > 200 else '')
            fields.append({'name': '🔁  Steps to Reproduce', 'value': steps, 'inline': False})
        if report.expected_behavior:
            fields.append({'name': '✅  Expected Behaviour', 'value': report.expected_behavior[:150], 'inline': False})

    fields.append(_divider())
    fields.append({'name': '📝  Description', 'value': f'>>> {desc_preview}', 'inline': False})
    fields.append(_divider())
    fields.append({
        'name':   '🔗  Quick Actions',
        'value':  f"[**Open in Admin Panel →**]({case_url})  •  [View as Reporter]({_user_report_url(report.id)})",
        'inline': False,
    })

    discord_mention = f'<@{reporter.discord_id}>' if reporter.discord_id else reporter.username

    _post_webhook({
        'content': f"📣  **New {meta['label']} submitted** — action required.",
        'embeds': [{
            'author': {
                'name':     f"{reporter.username}  ({reporter.discord_username or 'no Discord'})",
                'icon_url': avatar or 'https://cdn.discordapp.com/embed/avatars/0.png',
                'url':      f"{site}/profile/{reporter.username}",
            },
            'title':       f"{meta['emoji']}  {report.title}",
            'url':         case_url,
            'description': f"A new report has been submitted and is awaiting staff review.\n**Reporter:** {discord_mention}",
            'color':       color,
            'fields':      fields,
            'thumbnail':   {'url': meta['thumbnail']},
            'footer': {
                'text':     f"CFRP Report System  •  Case #{report.id}  •  {site}",
                'icon_url': 'https://cdn.discordapp.com/embed/avatars/1.png',
            },
            'timestamp':   report.created_at.isoformat(),
        }],
    })


def _send_status_update_webhook(report: Report, old_status: str, actor: User):
    site       = _site_url()
    case_url   = _admin_report_url(report.id)
    color      = _EMBED_COLORS.get(report.status, 0x6B7280)
    meta       = _TYPE_META.get(report.report_type, _TYPE_META['bug'])
    old_label  = Report.STATUS_LABELS.get(old_status, old_status.title())
    old_emoji  = _STATUS_EMOJI.get(old_status, '⚪')
    new_emoji  = _STATUS_EMOJI.get(report.status, '⚪')
    actor_av   = _actor_avatar(actor)

    _post_webhook({
        'embeds': [{
            'author': {
                'name':     f"Updated by {actor.username}",
                'icon_url': actor_av or 'https://cdn.discordapp.com/embed/avatars/0.png',
            },
            'title':       f"🔄  Status Update — {report.title}",
            'url':         case_url,
            'description': (
                f"The status of **Case #{report.id}** has been changed by "
                f"**{actor.username}**.\n\n"
                f"{old_emoji}  ~~{old_label}~~  →  {new_emoji}  **{report.status_label}**"
            ),
            'color':       color,
            'fields': [
                {'name': '📋  Case ID',      'value': f'`#{report.id}`',                                'inline': True},
                {'name': '📁  Type',         'value': f"{meta['emoji']}  {meta['label']}",              'inline': True},
                {'name': '👤  Reporter',      'value': report.reporter.username,                         'inline': True},
                {'name': '⬅️  Previous',     'value': f"{old_emoji}  {old_label}",                     'inline': True},
                {'name': '➡️  New Status',   'value': f"{new_emoji}  **{report.status_label}**",        'inline': True},
                {'name': '\u200b',           'value': '\u200b',                                          'inline': True},
                _divider(),
                {
                    'name':  '🔗  View Case',
                    'value': f"[Open in Admin Panel →]({case_url})  •  [Reporter View]({_user_report_url(report.id)})",
                    'inline': False,
                },
            ],
            'footer': {
                'text':     f"CFRP Report System  •  Case #{report.id}",
                'icon_url': 'https://cdn.discordapp.com/embed/avatars/1.png',
            },
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }],
    })


def _send_resolved_webhook(report: Report, actor: User):
    site      = _site_url()
    case_url  = _admin_report_url(report.id)
    meta      = _TYPE_META.get(report.report_type, _TYPE_META['bug'])
    actor_av  = _actor_avatar(actor)

    # Duration open
    if report.resolved_at and report.created_at:
        delta   = report.resolved_at - report.created_at.replace(tzinfo=timezone.utc) \
                  if report.created_at.tzinfo is None \
                  else report.resolved_at - report.created_at
        hours   = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        duration = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    else:
        duration = 'Unknown'

    staff_note = report.staff_note or '_No closing note provided._'

    _post_webhook({
        'content': f"✅  **Case #{report.id} has been resolved** by **{actor.username}**.",
        'embeds': [{
            'author': {
                'name':     f"Resolved by {actor.username}",
                'icon_url': actor_av or 'https://cdn.discordapp.com/embed/avatars/0.png',
            },
            'title':       f"✅  Resolved — {report.title}",
            'url':         case_url,
            'description': (
                f"**Case #{report.id}** has been marked as resolved.\n"
                f"The reporter has been notified automatically."
            ),
            'color':       0x22C55E,
            'fields': [
                {'name': '📋  Case ID',       'value': f'`#{report.id}`',             'inline': True},
                {'name': '📁  Type',          'value': f"{meta['emoji']}  {meta['label']}", 'inline': True},
                {'name': '👤  Reporter',       'value': report.reporter.username,       'inline': True},
                {'name': '🛡️  Resolved By',   'value': actor.username,                 'inline': True},
                {'name': '⏱️  Time Open',     'value': duration,                       'inline': True},
                {'name': '💬  Messages',      'value': str(report.messages.count()),   'inline': True},
                _divider(),
                {'name': '📌  Closing Note',  'value': f'>>> {staff_note[:500]}',      'inline': False},
                _divider(),
                {
                    'name':  '🔗  View Closed Case',
                    'value': f"[Admin Panel →]({case_url})  •  [Reporter View]({_user_report_url(report.id)})",
                    'inline': False,
                },
            ],
            'footer': {
                'text':     f"CFRP Report System  •  Case #{report.id}",
                'icon_url': 'https://cdn.discordapp.com/embed/avatars/1.png',
            },
            'timestamp': report.resolved_at.isoformat() if report.resolved_at else datetime.now(timezone.utc).isoformat(),
        }],
    })


# ─────────────────────────────────────────────────────────────────────────────
# USER-FACING ROUTES  (prefix: /reports)
# ─────────────────────────────────────────────────────────────────────────────

@reports_bp.route('/')
@login_required
def my_reports():
    page    = request.args.get('page', 1, type=int)
    status  = request.args.get('status', '')
    rtype   = request.args.get('type', '')

    q = Report.query.filter_by(user_id=current_user.id)
    if status:
        q = q.filter_by(status=status)
    if rtype:
        q = q.filter_by(report_type=rtype)

    pagination = q.order_by(Report.created_at.desc()).paginate(
        page=page, per_page=10, error_out=False)

    return render_template('reports/my_reports.html',
        reports=pagination.items, pagination=pagination,
        status_filter=status, type_filter=rtype,
        statuses=Report.STATUSES, status_labels=Report.STATUS_LABELS,
        status_colors=Report.STATUS_COLORS)


@reports_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new_report():
    if request.method == 'POST':
        rtype              = request.form.get('report_type', '').strip()
        title              = request.form.get('title', '').strip()
        description        = request.form.get('description', '').strip()
        reported_player    = request.form.get('reported_player', '').strip() or None
        reported_discord_id = request.form.get('reported_discord_id', '').strip() or None

        if rtype not in ('player', 'bug'):
            flash('Invalid report type.', 'error')
            return redirect(url_for('reports.new_report'))
        if not title or not description:
            flash('Title and description are required.', 'error')
            return redirect(url_for('reports.new_report'))

        # Auto-link reported user if their Discord ID is known
        reported_user_id = None
        if reported_discord_id:
            linked = User.query.filter_by(discord_id=reported_discord_id).first()
            if linked:
                reported_user_id = linked.id

        report = Report(
            user_id             = current_user.id,
            report_type         = rtype,
            title               = title,
            description         = description,
            reported_player     = reported_player,
            reported_user_id    = reported_user_id,
            server_id           = request.form.get('server_id', '').strip() or None,
            steps_to_reproduce  = request.form.get('steps_to_reproduce', '').strip() or None,
            expected_behavior   = request.form.get('expected_behavior', '').strip() or None,
        )
        db.session.add(report)
        db.session.flush()   # get ID before commit

        # Notify all staff
        staff = _get_staff_users()
        for s in staff:
            db.session.add(Notification(
                user_id = s.id,
                title   = f'New {report.report_type.title()} Report',
                message = f'{current_user.username} submitted report #{report.id}: {report.title}',
                type    = 'warning',
                link    = url_for('reports.admin_report_detail', report_id=report.id),
            ))

        AuditLog.log('report.create', user_id=current_user.id,
                     resource_type='report', resource_id=report.id,
                     details={'type': rtype, 'title': title})
        db.session.commit()

        _send_new_report_webhook(report)
        _dm_report_received(report)                          # ← DM reporter
        if reported_user_id:
            _dm_reported_player_notice(report)               # ← auto-DM reported user if linked

        flash('Your report has been submitted. We\'ll get back to you soon.', 'success')
        return redirect(url_for('reports.view_report', report_id=report.id))

    return render_template('reports/new_report.html')


@reports_bp.route('/<int:report_id>')
@login_required
def view_report(report_id):
    report = Report.query.get_or_404(report_id)

    # Only the reporter or staff can view
    if report.user_id != current_user.id and not current_user.has_permission('admin.access'):
        abort(403)

    # Mark staff messages as read when reporter views
    if report.user_id == current_user.id:
        unread = report.messages.filter_by(is_staff=True, is_read=False).all()
        for m in unread:
            m.is_read = True
        if unread:
            db.session.commit()

    messages = report.messages.order_by(ReportMessage.created_at.asc()).all()

    return render_template('reports/view_report.html',
        report=report, messages=messages,
        statuses=Report.STATUSES, status_labels=Report.STATUS_LABELS,
        status_colors=Report.STATUS_COLORS, status_icons=Report.STATUS_ICONS)


@reports_bp.route('/<int:report_id>/message', methods=['POST'])
@login_required
def post_message(report_id):
    report = Report.query.get_or_404(report_id)

    is_staff = current_user.has_permission('admin.access')

    if report.user_id != current_user.id and not is_staff:
        abort(403)

    content = request.form.get('content', '').strip()
    if not content:
        flash('Message cannot be empty.', 'error')
        return redirect(url_for('reports.view_report', report_id=report_id))

    msg = ReportMessage(
        report_id = report_id,
        user_id   = current_user.id,
        content   = content,
        is_staff  = is_staff,
        is_read   = is_staff,   # staff messages start unread for reporter; user msgs start "read"
    )
    db.session.add(msg)

    # Auto-bump status to 'seen' when staff first replies
    if is_staff and report.status == 'pending':
        report.status = 'seen'

    db.session.commit()

    # Notify the other party
    if is_staff:
        # Staff replied → DM the reporter
        _dm_staff_reply(report, current_user.username, content)
        db.session.add(Notification(
            user_id = report.user_id,
            title   = f'Staff replied to your report #{report.id}',
            message = f'{current_user.username}: {content[:120]}',
            type    = 'info',
            link    = url_for('reports.view_report', report_id=report_id),
        ))
    else:
        # Notify assigned staff or all admins
        recipients = [report.assignee] if report.assignee else _get_staff_users()
        for s in recipients:
            db.session.add(Notification(
                user_id = s.id,
                title   = f'User replied on report #{report.id}',
                message = f'{current_user.username}: {content[:120]}',
                type    = 'info',
                link    = url_for('reports.admin_report_detail', report_id=report_id),
            ))

    db.session.commit()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})

    return redirect(url_for('reports.view_report', report_id=report_id) + '#chat')


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ROUTES  (prefix: /reports/admin)
# ─────────────────────────────────────────────────────────────────────────────

@reports_bp.route('/admin')
@login_required
@_require_staff
def admin_reports():
    page        = request.args.get('page', 1, type=int)
    status      = request.args.get('status', '')
    rtype       = request.args.get('type', '')
    assigned_me = request.args.get('mine', '')

    q = Report.query
    if status:
        q = q.filter_by(status=status)
    if rtype:
        q = q.filter_by(report_type=rtype)
    if assigned_me:
        q = q.filter_by(assigned_to=current_user.id)

    pagination = q.order_by(Report.created_at.desc()).paginate(
        page=page, per_page=20, error_out=False)

    counts = {
        'total':       Report.query.count(),
        'pending':     Report.query.filter_by(status='pending').count(),
        'in_progress': Report.query.filter_by(status='in_progress').count(),
        'resolved':    Report.query.filter_by(status='resolved').count(),
    }

    return render_template('admin/reports.html',
        reports=pagination.items, pagination=pagination,
        counts=counts, status_filter=status, type_filter=rtype, mine=assigned_me,
        statuses=Report.STATUSES, status_labels=Report.STATUS_LABELS,
        status_colors=Report.STATUS_COLORS)


@reports_bp.route('/admin/<int:report_id>')
@login_required
@_require_staff
def admin_report_detail(report_id):
    report          = Report.query.get_or_404(report_id)
    messages        = report.messages.order_by(ReportMessage.created_at.asc()).all()
    suspect_messages = report.suspect_messages.order_by(ReportSuspectMessage.created_at.asc()).all() if report.report_type == 'player' else []
    staff           = _get_staff_users()

    # Mark reporter messages as read when staff views
    for m in report.messages.filter_by(is_staff=False, is_read=False).all():
        m.is_read = True

    # Auto-advance from pending → seen
    if report.status == 'pending':
        report.status = 'seen'

    db.session.commit()

    return render_template('admin/report_detail.html',
        report=report, messages=messages, suspect_messages=suspect_messages, staff=staff,
        statuses=Report.STATUSES, status_labels=Report.STATUS_LABELS,
        status_colors=Report.STATUS_COLORS, status_icons=Report.STATUS_ICONS)


@reports_bp.route('/admin/<int:report_id>/update', methods=['POST'])
@login_required
@_require_staff
def admin_update_report(report_id):
    report     = Report.query.get_or_404(report_id)
    old_status = report.status

    new_status   = request.form.get('status', report.status)
    assigned_to  = request.form.get('assigned_to', '')
    staff_note   = request.form.get('staff_note', '').strip()

    if new_status in Report.STATUSES:
        report.status = new_status
    if assigned_to:
        report.assigned_to = int(assigned_to) if assigned_to != '0' else None
    if staff_note is not None:
        report.staff_note = staff_note or None

    # Handle resolve
    if report.status == 'resolved' and old_status != 'resolved':
        report.resolved_at = datetime.now(timezone.utc)
        report.resolved_by = current_user.id

        # Notify reporter
        db.session.add(Notification(
            user_id = report.user_id,
            title   = f'Your report #{report.id} has been resolved',
            message = f'Staff note: {staff_note or "Case completed."}',
            type    = 'success',
            link    = url_for('reports.view_report', report_id=report.id),
        ))
        db.session.commit()
        _send_resolved_webhook(report, current_user)
        _dm_resolved(report, current_user.username)             # ← DM reporter: resolved
        _dm_reported_player_resolved(report)                    # ← DM reported user: resolved

    elif report.status != old_status:
        # Notify reporter of status change
        db.session.add(Notification(
            user_id = report.user_id,
            title   = f'Your report #{report.id} status changed',
            message = f'Status updated to: {report.status_label}',
            type    = 'info',
            link    = url_for('reports.view_report', report_id=report.id),
        ))
        db.session.commit()
        _send_status_update_webhook(report, old_status, current_user)
        _dm_status_update(report, old_status, current_user.username)   # ← DM reporter: status change

    AuditLog.log('report.update', user_id=current_user.id,
                 resource_type='report', resource_id=report.id,
                 details={'old_status': old_status, 'new_status': report.status})
    db.session.commit()

    flash('Report updated.', 'success')
    return redirect(url_for('reports.admin_report_detail', report_id=report_id))


@reports_bp.route('/admin/<int:report_id>/message', methods=['POST'])
@login_required
@_require_staff
def admin_post_message(report_id):
    """Staff reply to a report — also accessible from admin detail page."""
    report  = Report.query.get_or_404(report_id)
    content = request.form.get('content', '').strip()
    if not content:
        flash('Message cannot be empty.', 'error')
        return redirect(url_for('reports.admin_report_detail', report_id=report_id))

    msg = ReportMessage(
        report_id = report_id,
        user_id   = current_user.id,
        content   = content,
        is_staff  = True,
        is_read   = False,
    )
    db.session.add(msg)

    # Auto-bump to 'seen' on first staff reply
    if report.status == 'pending':
        report.status = 'seen'

    # Notify reporter
    db.session.add(Notification(
        user_id = report.user_id,
        title   = f'Staff replied to your report #{report.id}',
        message = f'{current_user.username}: {content[:120]}',
        type    = 'info',
        link    = url_for('reports.view_report', report_id=report_id),
    ))
    db.session.commit()

    _dm_staff_reply(report, current_user.username, content)  # ← DM the reporter

    flash('Reply sent.', 'success')
    return redirect(url_for('reports.admin_report_detail', report_id=report_id) + '#chat')


@reports_bp.route('/admin/<int:report_id>/delete', methods=['POST'])
@login_required
@_require_staff
def admin_delete_report(report_id):
    report = Report.query.get_or_404(report_id)
    AuditLog.log('report.delete', user_id=current_user.id,
                 resource_type='report', resource_id=report_id)
    db.session.delete(report)
    db.session.commit()
    flash('Report deleted.', 'success')
    return redirect(url_for('reports.admin_reports'))


@reports_bp.route('/admin/<int:report_id>/link-user', methods=['POST'])
@login_required
@_require_staff
def admin_link_reported_user(report_id):
    """Link a website user account to the reported_user field and DM them the investigation notice."""
    report = Report.query.get_or_404(report_id)
    if report.report_type != 'player':
        flash('Only player reports can have a reported user linked.', 'error')
        return redirect(url_for('reports.admin_report_detail', report_id=report_id))

    username = request.form.get('reported_username', '').strip()
    if not username:
        flash('Username is required.', 'error')
        return redirect(url_for('reports.admin_report_detail', report_id=report_id))

    user = User.query.filter_by(username=username).first()
    if not user:
        flash(f'No user found with username "{username}".', 'error')
        return redirect(url_for('reports.admin_report_detail', report_id=report_id))

    already_linked = report.reported_user_id == user.id
    report.reported_user_id = user.id
    AuditLog.log('report.link_user', user_id=current_user.id,
                 resource_type='report', resource_id=report.id,
                 details={'linked_user': username})
    db.session.commit()

    if not already_linked:
        _dm_reported_player_notice(report)   # ← DM them the investigation notice
        flash(f'Linked {username} as the reported user and sent them a Discord DM notice.', 'success')
    else:
        flash(f'{username} was already linked — no DM sent.', 'info')

    return redirect(url_for('reports.admin_report_detail', report_id=report_id))


@reports_bp.route('/admin/<int:report_id>/unlink-user', methods=['POST'])
@login_required
@_require_staff
def admin_unlink_reported_user(report_id):
    """Remove the linked reported user from a report."""
    report = Report.query.get_or_404(report_id)
    report.reported_user_id = None
    db.session.commit()
    flash('Reported user unlinked.', 'success')
    return redirect(url_for('reports.admin_report_detail', report_id=report_id))


@reports_bp.route('/admin/<int:report_id>/suspect-message', methods=['POST'])
@login_required
@_require_staff
def admin_post_suspect_message(report_id):
    """Staff sends a message to the reported (suspect) user."""
    report  = Report.query.get_or_404(report_id)
    content = request.form.get('content', '').strip()

    if not content:
        flash('Message cannot be empty.', 'error')
        return redirect(url_for('reports.admin_report_detail', report_id=report_id))

    if not report.reported_user_id:
        flash('No reported user linked to this report. Link a user first.', 'error')
        return redirect(url_for('reports.admin_report_detail', report_id=report_id))

    msg = ReportSuspectMessage(
        report_id = report_id,
        user_id   = current_user.id,
        content   = content,
        is_staff  = True,
        is_read   = False,
    )
    db.session.add(msg)

    # Site notification to reported user
    db.session.add(Notification(
        user_id = report.reported_user_id,
        title   = f'Staff message regarding Case #{report.id}',
        message = f'{current_user.username}: {content[:120]}',
        type    = 'warning',
        link    = url_for('reports.view_report', report_id=report_id),
    ))
    db.session.commit()

    _dm_reported_player_staff_message(report, current_user.username, content)  # ← DM reported user

    flash('Message sent to reported user.', 'success')
    return redirect(url_for('reports.admin_report_detail', report_id=report_id) + '#suspect-chat')


# ─────────────────────────────────────────────────────────────────────────────
# JSON endpoints
# ─────────────────────────────────────────────────────────────────────────────

@reports_bp.route('/admin/count')
@login_required
@_require_staff
def admin_report_count():
    """Used by sidebar badge."""
    count = Report.query.filter_by(status='pending').count()
    return jsonify({'pending': count})


@reports_bp.route('/api/member-search')
@login_required
def member_search():
    """Search Discord guild members by username. Returns JSON list for the player picker."""
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])

    bot_token = current_app.config.get('DISCORD_BOT_TOKEN', '')
    guild_id  = current_app.config.get('DISCORD_GUILD_ID', '')
    if not bot_token or not guild_id:
        return jsonify([])

    try:
        r = requests.get(
            f"https://discord.com/api/v10/guilds/{guild_id}/members/search",
            headers={"Authorization": f"Bot {bot_token}"},
            params={"query": q, "limit": 15},
            timeout=6,
        )
        if r.status_code != 200:
            current_app.logger.warning(f"Member search failed {r.status_code}: {r.text[:200]}")
            return jsonify([])

        members = []
        for m in r.json():
            user = m.get('user', {})
            uid  = user.get('id', '')
            name = m.get('nick') or user.get('global_name') or user.get('username', '')
            tag  = user.get('username', '')
            av   = user.get('avatar')
            avatar_url = (
                f"https://cdn.discordapp.com/avatars/{uid}/{av}.png?size=32"
                if av else
                f"https://cdn.discordapp.com/embed/avatars/{int(uid) % 5}.png"
            )
            members.append({
                'id':         uid,
                'name':       name,
                'username':   tag,
                'avatar_url': avatar_url,
                'display':    f"{name} (@{tag})" if name != tag else f"@{tag}",
            })
        return jsonify(members)
    except Exception as exc:
        current_app.logger.error(f"Member search error: {exc}")
        return jsonify([])