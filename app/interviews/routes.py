"""
Interview scheduling system.
 - Users book a 30-minute slot between 08:30–22:30 SAST on a date they choose.
 - Staff confirm, reassign, complete, or cancel interviews from the admin panel.
 - Discord DMs are sent for every status change.
"""

import requests
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import (
    render_template, redirect, url_for, flash,
    request, jsonify, current_app, abort,
)
from flask_login import login_required, current_user

from app.interviews import interviews_bp
from app.models import db, Interview, InterviewSlot, Notification, AuditLog, User

SAST = ZoneInfo("Africa/Johannesburg")

# Slot generation — 08:30 to 22:30 in 30-min steps
SLOT_START = time(8, 30)
SLOT_END   = time(22, 30)


def _all_slot_times() -> list[time]:
    slots = []
    h, m = SLOT_START.hour, SLOT_START.minute
    while True:
        t = time(h, m)
        slots.append(t)
        if t >= SLOT_END:
            break
        m += 30
        if m >= 60:
            h += 1
            m -= 60
    return slots


ALL_SLOTS = _all_slot_times()


def _get_staff():
    return [u for u in User.query.all() if u.is_admin or u.has_permission('admin.access')]


def _require_staff(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not (current_user.is_admin or current_user.has_permission('admin.access')):
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ── Discord DM helpers ─────────────────────────────────────────────────────────

DISCORD_API = "https://discord.com/api/v10"


def _bot_headers():
    return {
        "Authorization": f"Bot {current_app.config.get('DISCORD_BOT_TOKEN', '')}",
        "Content-Type": "application/json",
    }


import os as _os

# Discord webhook that receives new interview booking notifications
_INTERVIEW_WEBHOOK = _os.environ.get(
    "INTERVIEW_DISCORD_WEBHOOK",
    "https://discord.com/api/webhooks/1501616538824871997/xd1Eu7cNV7yxkrElw-vqTqh4j9w14SMxTQU0U7eV4g0_jte8zF9Qi_5GJQPe794FlnoM",
)


def _build_interview_embed(interview) -> dict:
    """Shared embed builder used by both the staff webhook and ticket channel posts."""
    u          = interview.applicant
    username   = u.username   if u else "Unknown"
    discord_id = u.discord_id if u else ""
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{u.discord_id}/{u.avatar}.png"
        if u and u.discord_id and getattr(u, "avatar", None)
        else ""
    )

    fields = [
        {"name": "\U0001f4cb  Booking ID",  "value": f"```#{interview.id}```",                                       "inline": True},
        {"name": "\U0001f464  Applicant",   "value": f"```{username}```",                                            "inline": True},
        {"name": "\U0001f517  Discord",     "value": f"<@{discord_id}>" if discord_id else "*Not linked*",           "inline": True},
        {"name": "\U0001f4c5  Date",        "value": f"```{interview.interview_date.strftime('%A, %d %b %Y')}```",   "inline": True},
        {"name": "\u23f0  Time (SAST)",     "value": f"```{interview.interview_time.strftime('%H:%M')} SAST```",     "inline": True},
        {"name": "\u200b",                  "value": "\u200b",                                                       "inline": True},
    ]

    if interview.notes:
        fields.append({
            "name":   "\U0001f4dd  Notes",
            "value":  f"> {interview.notes[:500]}",
            "inline": False,
        })

    from datetime import timezone as _tz
    embed = {
        "title":     "\U0001f4c5  Interview Booked",
        "color":     0x6366F1,
        "fields":    fields,
        "footer":    {"text": f"Cape Flats Roleplay  \u2022  Interview #{interview.id}"},
        "timestamp": datetime.now(_tz.utc).isoformat(),
    }
    if avatar_url:
        embed["thumbnail"] = {"url": avatar_url}
    return embed


def _notify_staff_webhook(interview):
    """POST a rich embed to the staff interview Discord webhook."""
    if not _INTERVIEW_WEBHOOK:
        return

    embed = _build_interview_embed(interview)
    embed["fields"].append({
        "name":   "\U0001f527  Actions",
        "value":  "[**View in Admin Panel \u2192**](https://web.goldenshoresrp.com/interviews/admin)\n"
                  "-# Confirm, reassign, or cancel from the admin panel.",
        "inline": False,
    })

    payload = {
        "content":  "\U0001f5d3\ufe0f A new interview has been booked!",
        "embeds":   [embed],
        "username": "CFRP Interviews",
    }

    try:
        resp = requests.post(_INTERVIEW_WEBHOOK, json=payload, timeout=8)
        resp.raise_for_status()
    except Exception as exc:
        current_app.logger.error("Interview webhook notify failed: %s", exc)


def _post_to_ticket_channel(interview):
    """
    If the applicant has an open ticket in the Discord bot's database,
    post their interview confirmation into that ticket channel.
    """
    u = interview.applicant
    if not u or not u.discord_id:
        return

    bot_token = current_app.config.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        return

    # ── Query the bot's MySQL DB for an open ticket owned by this Discord user ──
    try:
        import pymysql
        from pymysql.cursors import DictCursor
        conn = pymysql.connect(
            host     = _os.environ.get("BOT_DB_HOST",     "localhost"),
            port     = int(_os.environ.get("BOT_DB_PORT", "3306")),
            user     = _os.environ.get("BOT_DB_USER",     _os.environ.get("DB_USER", "")),
            password = _os.environ.get("BOT_DB_PASSWORD", _os.environ.get("DB_PASSWORD", "")),
            db       = _os.environ.get("BOT_DB_NAME",     "cfrp_bot"),
            charset  = "utf8mb4",
            cursorclass = DictCursor,
            connect_timeout = 4,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT channel_id, ticket_number FROM tickets "
                "WHERE user_id = %s AND status = 'open' "
                "ORDER BY opened_at DESC LIMIT 1",
                (str(u.discord_id),)
            )
            row = cur.fetchone()
        conn.close()
    except Exception as exc:
        current_app.logger.error("Ticket DB lookup failed: %s", exc)
        return

    if not row:
        return  # No open ticket — nothing to post

    channel_id    = row["channel_id"]
    ticket_number = row.get("ticket_number", "?")

    # ── Build the ticket-channel embed ───────────────────────────────────────
    embed = _build_interview_embed(interview)
    embed["title"] = "\U0001f4c5  Interview Confirmation"
    embed["description"] = (
        f"Hey <@{u.discord_id}>! Your interview has been **booked**. "
        f"Staff will confirm shortly."
    )
    embed["fields"].append({
        "name":   "\U0001f517  Booking Page",
        "value":  "[View your interview \u2192](https://web.goldenshoresrp.com/interviews/my)",
        "inline": False,
    })

    payload = {
        "content": f"<@{u.discord_id}> — your interview has been booked! \U0001f4c5",
        "embeds":  [embed],
    }

    try:
        resp = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type":  "application/json",
            },
            json    = payload,
            timeout = 8,
        )
        resp.raise_for_status()
        current_app.logger.info(
            "Interview #%s confirmation posted to ticket %s (channel %s)",
            interview.id, ticket_number, channel_id,
        )
    except Exception as exc:
        current_app.logger.error(
            "Failed to post interview confirmation to ticket channel %s: %s",
            channel_id, exc,
        )


def _open_dm(discord_id: str) -> str | None:
    try:
        r = requests.post(
            f"{DISCORD_API}/users/@me/channels",
            headers=_bot_headers(),
            json={"recipient_id": discord_id},
            timeout=8,
        )
        if r.status_code in (200, 201):
            return r.json().get("id")
    except Exception as e:
        current_app.logger.error(f"Interview DM channel error: {e}")
    return None


def _send_dm(discord_id: str, embed: dict, content: str = ""):
    if not discord_id:
        return
    channel = _open_dm(discord_id)
    if not channel:
        return
    try:
        requests.post(
            f"{DISCORD_API}/channels/{channel}/messages",
            headers=_bot_headers(),
            json={"content": content, "embeds": [embed]},
            timeout=8,
        )
    except Exception as e:
        current_app.logger.error(f"Interview DM send error: {e}")


def _dm_booked(interview: Interview):
    u = interview.applicant
    if not u or not u.discord_id:
        return
    embed = {
        "author": {"name": "Cape Flats Roleplay — Interview System",
                   "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png"},
        "title": "📅  Interview Scheduled",
        "description": (
            f"Hey **{u.username}**, your interview has been booked!\n"
            "Staff will confirm your slot shortly. Make sure you're available on Discord at the scheduled time."
        ),
        "color": 0x6366F1,
        "fields": [
            {"name": "📋  Booking ID",  "value": f"```#{interview.id}```",            "inline": True},
            {"name": "📅  Date",        "value": f"```{interview.interview_date.strftime('%A, %d %b %Y')}```", "inline": True},
            {"name": "⏰  Time (SAST)", "value": f"```{interview.interview_time.strftime('%H:%M')} SAST```",   "inline": True},
            {"name": "🔔  Status",      "value": "```🟡 Pending Confirmation```",      "inline": True},
            {"name": "📝  Your Notes",  "value": f"> {interview.notes[:500]}" if interview.notes else "> —", "inline": False},
            {"name": "ℹ️  Next Steps",
             "value": "A staff member will confirm your booking. You'll receive a DM when confirmed.\n"
                      "-# If you need to cancel, visit the website.",
             "inline": False},
        ],
        "footer": {"text": f"Cape Flats Roleplay  •  Interview #{interview.id}",
                   "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _send_dm(u.discord_id, embed,
             content=f"<@{u.discord_id}> — Your interview has been scheduled! 📅")


def _dm_confirmed(interview: Interview):
    u = interview.applicant
    if not u or not u.discord_id:
        return
    staff_name = interview.interviewer.username if interview.interviewer else "Staff"
    embed = {
        "author": {"name": f"Confirmed by {staff_name}  •  CFRP Staff",
                   "icon_url": "https://cdn.discordapp.com/embed/avatars/0.png"},
        "title": "✅  Interview Confirmed",
        "description": (
            f"Your interview has been **confirmed** by staff.\n"
            "Please be ready on Discord at the time below. Join the waiting room and a staff member will reach out."
        ),
        "color": 0x22C55E,
        "fields": [
            {"name": "📋  Booking ID",       "value": f"```#{interview.id}```",            "inline": True},
            {"name": "👤  Your Interviewer", "value": f"```{staff_name}```",                "inline": True},
            {"name": "📅  Date",             "value": f"```{interview.interview_date.strftime('%A, %d %b %Y')}```", "inline": True},
            {"name": "⏰  Time (SAST)",      "value": f"```{interview.interview_time.strftime('%H:%M')} SAST```",   "inline": True},
            {"name": "⚠️  Important",
             "value": "Be on Discord **5 minutes early**. If you're more than 10 minutes late without notice, your slot may be forfeited.",
             "inline": False},
        ],
        "footer": {"text": f"Cape Flats Roleplay  •  Interview #{interview.id}",
                   "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _send_dm(u.discord_id, embed,
             content=f"<@{u.discord_id}> — Your interview is **confirmed**! ✅")


def _dm_cancelled(interview: Interview, reason: str, by_staff: bool):
    u = interview.applicant
    if not u or not u.discord_id:
        return
    color = 0xEF4444 if by_staff else 0x6B7280
    embed = {
        "author": {"name": "Cape Flats Roleplay — Interview System",
                   "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png"},
        "title": "❌  Interview Cancelled",
        "description": (
            f"Your interview (#{interview.id}) has been **cancelled**"
            f" {'by a staff member' if by_staff else 'at your request'}."
        ),
        "color": color,
        "fields": [
            {"name": "📋  Booking ID",  "value": f"```#{interview.id}```",            "inline": True},
            {"name": "📅  Was Scheduled For", "value": f"```{interview.datetime_sast}```", "inline": True},
            {"name": "📝  Reason",      "value": f"> {reason[:500]}" if reason else "> No reason provided.", "inline": False},
            {"name": "🔄  Reschedule",
             "value": "[Book a new interview slot →](https://web.goldenshoresrp.com/interviews/book)\n"
                      "-# You're welcome to reschedule at any time.",
             "inline": False},
        ],
        "footer": {"text": f"Cape Flats Roleplay  •  Interview #{interview.id}",
                   "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _send_dm(u.discord_id, embed,
             content=f"<@{u.discord_id}> — Your interview has been cancelled.")


def _dm_completed(interview: Interview):
    u = interview.applicant
    if not u or not u.discord_id:
        return
    embed = {
        "author": {"name": "Cape Flats Roleplay — Interview System",
                   "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png"},
        "title": "🎉  Interview Completed",
        "description": (
            "Your interview has been marked as **completed** by staff.\n"
            "Thank you for taking the time — you'll be notified of the outcome shortly."
        ),
        "color": 0x22C55E,
        "fields": [
            {"name": "📋  Booking ID", "value": f"```#{interview.id}```", "inline": True},
            {"name": "📅  Date",       "value": f"```{interview.datetime_sast}```", "inline": True},
        ],
        "footer": {"text": f"Cape Flats Roleplay  •  Interview #{interview.id}",
                   "icon_url": "https://cdn.discordapp.com/embed/avatars/1.png"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _send_dm(u.discord_id, embed,
             content=f"<@{u.discord_id}> — Your interview has been completed! 🎉")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _booked_slots_for_date(target_date: date) -> set[time]:
    """Return set of times already booked (confirmed/pending) on a date."""
    rows = Interview.query.filter(
        Interview.interview_date == target_date,
        Interview.status.in_(['pending', 'confirmed']),
    ).all()
    return {r.interview_time for r in rows}


def _blocked_slots_for_date(target_date: date) -> set[time]:
    """Return set of times blocked by staff for a specific date."""
    rows = InterviewSlot.query.filter(
        InterviewSlot.is_blocked == True,
        db.or_(
            InterviewSlot.slot_date == target_date,
            InterviewSlot.slot_date == None,
        )
    ).all()
    return {r.slot_time for r in rows}


def _available_slots(target_date: date) -> list[time]:
    """Return list of available slot times for a given date."""
    booked  = _booked_slots_for_date(target_date)
    blocked = _blocked_slots_for_date(target_date)
    taken   = booked | blocked

    # Don't allow booking in the past
    now_sast = datetime.now(SAST)
    cutoff   = (now_sast + timedelta(hours=1)).time() if target_date == now_sast.date() else None

    result = []
    for t in ALL_SLOTS:
        if t in taken:
            continue
        if cutoff and t <= cutoff:
            continue
        result.append(t)
    return result


# ── User-facing routes ─────────────────────────────────────────────────────────

@interviews_bp.route('/')
@login_required
def my_interviews():
    interviews = Interview.query.filter_by(user_id=current_user.id)\
        .order_by(Interview.interview_date.desc(), Interview.interview_time.desc()).all()
    return render_template('interviews/my_interviews.html', interviews=interviews)


@interviews_bp.route('/book', methods=['GET', 'POST'])
@login_required
def book():
    # Check user doesn't already have a pending/confirmed booking
    existing = Interview.query.filter(
        Interview.user_id == current_user.id,
        Interview.status.in_(['pending', 'confirmed']),
    ).first()
    if existing:
        flash(f'You already have an active interview booking (#{existing.id}). '
              'Please cancel it before scheduling a new one.', 'warning')
        return redirect(url_for('interviews.my_interviews'))

    if request.method == 'POST':
        date_str  = request.form.get('interview_date', '').strip()
        time_str  = request.form.get('interview_time', '').strip()
        notes     = request.form.get('notes', '').strip()

        try:
            chosen_date = date.fromisoformat(date_str)
        except ValueError:
            flash('Invalid date.', 'error')
            return redirect(url_for('interviews.book'))

        try:
            chosen_time = time.fromisoformat(time_str)
        except ValueError:
            flash('Invalid time.', 'error')
            return redirect(url_for('interviews.book'))

        # Validate date is not in the past
        now_sast = datetime.now(SAST).date()
        if chosen_date < now_sast:
            flash('Cannot book a date in the past.', 'error')
            return redirect(url_for('interviews.book'))

        # Validate time is in our allowed slot list
        if chosen_time not in ALL_SLOTS:
            flash('Invalid time slot selected.', 'error')
            return redirect(url_for('interviews.book'))

        # Validate slot is still available
        available = _available_slots(chosen_date)
        if chosen_time not in available:
            flash('That slot is no longer available. Please choose another.', 'error')
            return redirect(url_for('interviews.book'))

        interview = Interview(
            user_id        = current_user.id,
            interview_date = chosen_date,
            interview_time = chosen_time,
            notes          = notes or None,
            status         = 'pending',
        )
        db.session.add(interview)

        # Notify staff
        staff = _get_staff()
        for s in staff:
            db.session.add(Notification(
                user_id = s.id,
                title   = 'New Interview Booking',
                message = f'{current_user.username} booked an interview for '
                          f'{chosen_date.strftime("%d %b")} at {chosen_time.strftime("%H:%M")} SAST',
                type    = 'info',
                link    = url_for('interviews.admin_interviews'),
            ))

        AuditLog.log('interview.book', user_id=current_user.id,
                     resource_type='interview',
                     details={'date': date_str, 'time': time_str})
        db.session.commit()
        _dm_booked(interview)
        _notify_staff_webhook(interview)
        _post_to_ticket_channel(interview)

        flash(f'Interview booked for {chosen_date.strftime("%A, %d %b %Y")} at '
              f'{chosen_time.strftime("%H:%M")} SAST! Staff will confirm shortly.', 'success')
        return redirect(url_for('interviews.my_interviews'))

    # GET — default to tomorrow
    tomorrow = datetime.now(SAST).date() + timedelta(days=1)
    return render_template('interviews/book.html',
                           default_date=tomorrow.isoformat(),
                           min_date=datetime.now(SAST).date().isoformat())


@interviews_bp.route('/api/slots')
@login_required
def api_slots():
    """Return available slots for a given date as JSON."""
    date_str = request.args.get('date', '')
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({'error': 'invalid date'}), 400

    available = _available_slots(target)
    booked    = _booked_slots_for_date(target)
    blocked   = _blocked_slots_for_date(target)

    return jsonify({
        'date': date_str,
        'slots': [
            {
                'time':      t.strftime('%H:%M'),
                'available': t in available,
                'booked':    t in booked,
                'blocked':   t in blocked,
            }
            for t in ALL_SLOTS
        ],
    })


@interviews_bp.route('/cancel/<int:interview_id>', methods=['POST'])
@login_required
def cancel(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    if interview.user_id != current_user.id:
        abort(403)
    if interview.status in ('completed', 'cancelled', 'no_show'):
        flash('This interview cannot be cancelled.', 'error')
        return redirect(url_for('interviews.my_interviews'))

    reason = request.form.get('reason', '').strip()
    interview.status      = 'cancelled'
    interview.cancelled_by = current_user.id
    interview.cancel_reason = reason or 'Cancelled by user'
    AuditLog.log('interview.cancel', user_id=current_user.id,
                 resource_type='interview', resource_id=interview.id)
    db.session.commit()
    _dm_cancelled(interview, reason, by_staff=False)
    flash('Your interview has been cancelled.', 'success')
    return redirect(url_for('interviews.my_interviews'))


# ── Admin routes ───────────────────────────────────────────────────────────────

@interviews_bp.route('/admin')
@login_required
@_require_staff
def admin_interviews():
    status_filter = request.args.get('status', 'active')
    q = Interview.query

    if status_filter == 'active':
        q = q.filter(Interview.status.in_(['pending', 'confirmed']))
    elif status_filter in Interview.STATUSES:
        q = q.filter_by(status=status_filter)

    interviews = q.order_by(Interview.interview_date.asc(), Interview.interview_time.asc()).all()
    staff      = User.query.all()
    staff      = [u for u in staff if u.is_admin or u.has_permission('admin.access')]

    today   = datetime.now(SAST).date()
    upcoming = Interview.query.filter(
        Interview.interview_date >= today,
        Interview.status.in_(['pending', 'confirmed']),
    ).count()
    pending = Interview.query.filter_by(status='pending').count()

    return render_template('admin/interviews/list.html',
                           interviews=interviews, staff=staff,
                           status_filter=status_filter,
                           upcoming=upcoming, pending=pending,
                           statuses=Interview.STATUSES,
                           status_labels=Interview.STATUS_LABELS)


@interviews_bp.route('/admin/<int:interview_id>/update', methods=['POST'])
@login_required
@_require_staff
def admin_update(interview_id):
    interview    = Interview.query.get_or_404(interview_id)
    action       = request.form.get('action', '').strip()
    staff_notes  = request.form.get('staff_notes', '').strip()
    interviewer_id = request.form.get('interviewer_id', '').strip()

    if staff_notes:
        interview.staff_notes = staff_notes

    if interviewer_id:
        interview.interviewer_id = int(interviewer_id) if interviewer_id != '0' else None

    if action == 'confirm':
        interview.status = 'confirmed'
        if not interview.interviewer_id:
            interview.interviewer_id = current_user.id
        db.session.commit()
        _dm_confirmed(interview)
        flash('Interview confirmed and user notified.', 'success')

    elif action == 'complete':
        interview.status = 'completed'
        db.session.commit()
        _dm_completed(interview)
        flash('Interview marked as completed.', 'success')

    elif action == 'no_show':
        interview.status = 'no_show'
        db.session.commit()
        flash('Interview marked as no-show.', 'success')

    elif action == 'cancel':
        reason = request.form.get('cancel_reason', '').strip()
        interview.status       = 'cancelled'
        interview.cancelled_by = current_user.id
        interview.cancel_reason = reason or 'Cancelled by staff'
        db.session.commit()
        _dm_cancelled(interview, reason, by_staff=True)
        flash('Interview cancelled and user notified.', 'success')

    elif action == 'save':
        db.session.commit()
        flash('Interview updated.', 'success')

    AuditLog.log(f'interview.{action}', user_id=current_user.id,
                 resource_type='interview', resource_id=interview.id)
    return redirect(url_for('interviews.admin_interviews'))


@interviews_bp.route('/admin/block-slot', methods=['POST'])
@login_required
@_require_staff
def admin_block_slot():
    """Block a specific time slot (optionally on a specific date)."""
    data       = request.get_json(silent=True) or {}
    time_str   = data.get('time', '')
    date_str   = data.get('date')  # optional — None = recurring block
    note       = data.get('note', '')

    try:
        slot_time = time.fromisoformat(time_str)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid time'}), 400

    slot_date = None
    if date_str:
        try:
            slot_date = date.fromisoformat(date_str)
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid date'}), 400

    # Check if already blocked
    existing = InterviewSlot.query.filter_by(slot_time=slot_time, slot_date=slot_date).first()
    if existing:
        existing.is_blocked = True
        existing.blocked_by = current_user.id
        existing.block_note = note
    else:
        db.session.add(InterviewSlot(
            slot_time  = slot_time,
            slot_date  = slot_date,
            is_blocked = True,
            blocked_by = current_user.id,
            block_note = note,
        ))
    db.session.commit()
    return jsonify({'success': True})


@interviews_bp.route('/admin/unblock-slot', methods=['POST'])
@login_required
@_require_staff
def admin_unblock_slot():
    data     = request.get_json(silent=True) or {}
    time_str = data.get('time', '')
    date_str = data.get('date')

    try:
        slot_time = time.fromisoformat(time_str)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid time'}), 400

    slot_date = date.fromisoformat(date_str) if date_str else None
    row = InterviewSlot.query.filter_by(slot_time=slot_time, slot_date=slot_date, is_blocked=True).first()
    if row:
        db.session.delete(row)
        db.session.commit()
    return jsonify({'success': True})


@interviews_bp.route('/admin/calendar')
@login_required
@_require_staff
def admin_calendar():
    """Calendar view — returns interview data for a given month."""
    year  = request.args.get('year',  datetime.now(SAST).year,  type=int)
    month = request.args.get('month', datetime.now(SAST).month, type=int)

    from calendar import monthrange
    _, days_in_month = monthrange(year, month)
    month_start = date(year, month, 1)
    month_end   = date(year, month, days_in_month)

    interviews = Interview.query.filter(
        Interview.interview_date >= month_start,
        Interview.interview_date <= month_end,
        Interview.status.in_(['pending', 'confirmed']),
    ).all()

    events = [
        {
            'id':     i.id,
            'date':   i.interview_date.isoformat(),
            'time':   i.interview_time.strftime('%H:%M'),
            'user':   i.applicant.username,
            'status': i.status,
            'color':  i.status_color,
        }
        for i in interviews
    ]
    return jsonify(events)