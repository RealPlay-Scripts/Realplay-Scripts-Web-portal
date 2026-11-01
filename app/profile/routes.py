from datetime import datetime, timezone, timedelta
from flask import render_template, redirect, url_for, flash, request, jsonify, abort
from flask_login import login_required, current_user
from app.profile import profile_bp
from app.models import (db, User, Application, PlayerSession, PlayerStat,
                        Notification, AuditLog, APIKey, PlayerHeartbeat)
from app.utils import save_upload, time_ago
import json as _json


def fmt_duration(seconds):
    if not seconds or seconds < 0:
        return '0m'
    days    = int(seconds) // 86400
    hours   = (int(seconds) % 86400) // 3600
    minutes = (int(seconds) % 3600) // 60
    secs    = int(seconds) % 60
    parts = []
    if days:    parts.append(f'{days}d')
    if hours:   parts.append(f'{hours}h')
    if minutes: parts.append(f'{minutes}m')
    if secs and not days: parts.append(f'{secs}s')
    return ' '.join(parts) if parts else '0m'


@profile_bp.route('/<username>')
def view(username):
    user = User.query.filter_by(username=username).first_or_404()
    is_own = current_user.is_authenticated and current_user.id == user.id

    kills = deaths = 0
    kd = 0.0
    total_secs = live_secs = 0
    is_online = False
    current_session = None

    if user.discord_id:
        kills  = PlayerStat.query.filter_by(discord_id=user.discord_id, event_type='kill').count()
        deaths = PlayerStat.query.filter_by(discord_id=user.discord_id, event_type='death').count()
        kd     = round(kills / max(deaths, 1), 2)

        total_secs = db.session.query(
            db.func.coalesce(db.func.sum(PlayerSession.duration_seconds), 0)
        ).filter(
            PlayerSession.discord_id == user.discord_id,
            PlayerSession.duration_seconds.isnot(None)
        ).scalar() or 0

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=90)
        hb = PlayerHeartbeat.query.filter_by(discord_id=user.discord_id).first()
        if hb and hb.last_seen:
            last_seen = hb.last_seen if hb.last_seen.tzinfo else hb.last_seen.replace(tzinfo=timezone.utc)
            if last_seen >= cutoff:
                is_online = True
                current_session = PlayerSession.query.filter_by(
                    discord_id=user.discord_id
                ).filter(PlayerSession.leave_time.is_(None)).order_by(
                    PlayerSession.join_time.desc()
                ).first()
                if current_session:
                    join = current_session.join_time
                    if join.tzinfo is None:
                        join = join.replace(tzinfo=timezone.utc)
                    live_secs = max(0, int((datetime.now(timezone.utc) - join).total_seconds()))

    grand_total = total_secs + live_secs

    apps = []
    if is_own or (current_user.is_authenticated and current_user.is_admin):
        apps = Application.query.filter_by(user_id=user.id).order_by(Application.submitted_at.desc()).limit(5).all()

    recent_kills = []
    if user.discord_id:
        recent_kills = PlayerStat.query.filter_by(
            discord_id=user.discord_id, event_type='kill'
        ).order_by(PlayerStat.recorded_at.desc()).limit(10).all()

    # Crash / timeout history for profile tab
    crash_history = []
    unviewed_crashes = 0
    if user.discord_id:
        try:
            from app.models import CrashDiagnosis
            since = datetime.now(timezone.utc) - timedelta(days=30)
            crash_history = CrashDiagnosis.query.filter(
                CrashDiagnosis.discord_id == user.discord_id,
                CrashDiagnosis.detected_at >= since,
            ).order_by(CrashDiagnosis.detected_at.desc()).limit(20).all()
            unviewed_crashes = sum(1 for c in crash_history if not c.is_viewed_user)
            # Mark as viewed if own profile
            if is_own:
                for c in crash_history:
                    c.is_viewed_user = True
                db.session.commit()
        except Exception:
            pass

    return render_template('profile/view.html',
        profile_user=user, is_own=is_own,
        kills=kills, deaths=deaths, kd=kd,
        total_playtime=fmt_duration(grand_total),
        total_seconds=grand_total, live_seconds=live_secs,
        is_online=is_online, current_session=current_session,
        apps=apps, recent_kills=recent_kills, time_ago=time_ago,
        crash_history=crash_history, unviewed_crashes=unviewed_crashes,
    )


@profile_bp.route('/<username>/live-time')
def live_time(username):
    user = User.query.filter_by(username=username).first_or_404()
    if not user.discord_id:
        return jsonify({'online': False, 'seconds': 0})
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=90)
    hb = PlayerHeartbeat.query.filter_by(discord_id=user.discord_id).first()
    if not hb:
        return jsonify({'online': False, 'seconds': 0})
    last_seen = hb.last_seen if hb.last_seen.tzinfo else hb.last_seen.replace(tzinfo=timezone.utc)
    if last_seen < cutoff:
        return jsonify({'online': False, 'seconds': 0})
    session = PlayerSession.query.filter_by(
        discord_id=user.discord_id
    ).filter(PlayerSession.leave_time.is_(None)).order_by(
        PlayerSession.join_time.desc()
    ).first()
    live_secs = 0
    if session:
        join = session.join_time
        if join.tzinfo is None:
            join = join.replace(tzinfo=timezone.utc)
        live_secs = max(0, int((datetime.now(timezone.utc) - join).total_seconds()))
    historical = db.session.query(
        db.func.coalesce(db.func.sum(PlayerSession.duration_seconds), 0)
    ).filter(
        PlayerSession.discord_id == user.discord_id,
        PlayerSession.duration_seconds.isnot(None)
    ).scalar() or 0
    return jsonify({'online': True, 'live_seconds': live_secs, 'total_seconds': historical + live_secs})


@profile_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        bio = request.form.get('bio', '').strip()[:500]
        current_user.bio = bio
        current_user.timezone = request.form.get('timezone', 'UTC')
        if 'avatar' in request.files:
            file = request.files['avatar']
            if file and file.filename:
                path = save_upload(file, 'avatars', f'user_{current_user.id}_')
                if path:
                    current_user.avatar = path
        db.session.commit()
        flash('Profile updated.', 'success')
    api_keys = APIKey.query.filter_by(user_id=current_user.id, is_active=True).all()
    return render_template('profile/settings.html', api_keys=api_keys)


@profile_bp.route('/notifications')
@login_required
def notifications():
    page = request.args.get('page', 1, type=int)
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    notifs = Notification.query.filter_by(user_id=current_user.id)\
        .order_by(Notification.created_at.desc()).paginate(page=page, per_page=20, error_out=False)
    return render_template('profile/notifications.html', pagination=notifs, notifications=notifs.items)


@profile_bp.route('/api-keys/create', methods=['POST'])
@login_required
def create_api_key():
    name = request.form.get('name', '').strip()
    scopes = request.form.getlist('scopes')
    if not name:
        flash('API key name required.', 'error')
        return redirect(url_for('profile.settings'))
    if APIKey.query.filter_by(user_id=current_user.id, is_active=True).count() >= 10:
        flash('Maximum 10 API keys allowed.', 'error')
        return redirect(url_for('profile.settings'))
    key_obj, raw_key = APIKey.generate(current_user.id, name, scopes or ['read'])
    db.session.add(key_obj)
    db.session.commit()
    flash(f'API key created. Copy it now — it will not be shown again: {raw_key}', 'success')
    return redirect(url_for('profile.settings'))


@profile_bp.route('/api-keys/<int:key_id>/revoke', methods=['POST'])
@login_required
def revoke_api_key(key_id):
    key = APIKey.query.get_or_404(key_id)
    if key.user_id != current_user.id:
        abort(403)
    key.is_active = False
    db.session.commit()
    flash('API key revoked.', 'info')
    return redirect(url_for('profile.settings'))
