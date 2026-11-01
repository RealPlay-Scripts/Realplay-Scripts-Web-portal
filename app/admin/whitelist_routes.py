"""
WHITELIST INTEGRATION — admin/whitelist_routes.py
=================================================
Paste these routes into your existing app/admin/routes.py
(they use the existing require_admin decorator and admin_bp blueprint).

Also add this import near the top of admin/routes.py:
    from app.models import WhitelistSchedule
"""

from datetime import datetime, timezone
from flask import render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from app.admin import admin_bp
from app.models import db, SiteSettings, AuditLog, WhitelistSchedule
# require_admin is already defined in admin/routes.py


# ── Whitelist admin page ───────────────────────────────────────────────────

@admin_bp.route('/whitelist')
@login_required
@require_admin
def whitelist():
    enabled = SiteSettings.get('whitelist_enabled', True)
    schedules = WhitelistSchedule.query.order_by(
        WhitelistSchedule.scheduled_at.asc()
    ).all()
    return render_template('admin/whitelist.html',
                           whitelist_enabled=enabled,
                           schedules=schedules)


# ── AJAX: toggle whitelist instantly ──────────────────────────────────────

@admin_bp.route('/whitelist/toggle', methods=['POST'])
@login_required
@require_admin
def whitelist_toggle():
    current = SiteSettings.get('whitelist_enabled', True)
    new_state = not current
    SiteSettings.set('whitelist_enabled', new_state, 'bool',
                     'FiveM whitelist active', 'whitelist')
    log = AuditLog(
        user_id=current_user.id,
        action='whitelist.set_' + ('on' if new_state else 'off'),
        resource_type='setting',
        resource_id='whitelist_enabled',
    )
    db.session.add(log)
    db.session.commit()
    return jsonify({'enabled': new_state})


# ── AJAX: create schedule ─────────────────────────────────────────────────

@admin_bp.route('/whitelist/schedule', methods=['POST'])
@login_required
@require_admin
def whitelist_create_schedule():
    data = request.get_json(silent=True) or {}
    try:
        dt_str = data.get('scheduled_at', '')
        dt = datetime.fromisoformat(dt_str.replace('Z', ''))
        # Treat the submitted datetime as UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return jsonify({'error': 'Invalid scheduled_at'}), 400

    repeat = data.get('repeat_type', 'once')
    if repeat not in ('once', 'daily', 'weekly'):
        return jsonify({'error': 'Invalid repeat_type'}), 400

    s = WhitelistSchedule(
        label        = data.get('label', ''),
        enabled      = bool(data.get('enabled', True)),
        scheduled_at = dt,
        repeat_type  = repeat,
        created_by   = current_user.id,
    )
    db.session.add(s)
    db.session.commit()
    return jsonify(s.to_dict()), 201


# ── AJAX: delete schedule ─────────────────────────────────────────────────

@admin_bp.route('/whitelist/schedule/<int:schedule_id>', methods=['DELETE'])
@login_required
@require_admin
def whitelist_delete_schedule(schedule_id):
    s = WhitelistSchedule.query.get_or_404(schedule_id)
    db.session.delete(s)
    db.session.commit()
    return jsonify({'deleted': schedule_id})
