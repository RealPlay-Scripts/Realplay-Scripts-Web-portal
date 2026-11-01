from datetime import datetime, timezone, timedelta
from flask import request, jsonify, current_app
from flask_login import current_user, login_required
from app.api import api_bp
from app.models import (db, User, Application, ApplicationType, PlayerSession,
                        PlayerStat, PlayerHeartbeat, PlaytimeAggregate,
                        Notification, AuditLog, Role, Webhook)
from app.utils import api_key_required, format_duration, time_ago


# ─── Notifications (authenticated) ───────────────────────────────────────────

@api_bp.route('/notifications')
@login_required
def get_notifications():
    notifs = Notification.query.filter_by(user_id=current_user.id)\
        .order_by(Notification.created_at.desc()).limit(20).all()
    return jsonify({'notifications': [n.to_dict() for n in notifs]})


@api_bp.route('/notifications/mark-read', methods=['POST'])
@login_required
def mark_notifications_read():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'success': True})


# ─── FiveM Server Ingestion (Bot → API) ──────────────────────────────────────

@api_bp.route('/server/heartbeat', methods=['POST'])
@api_key_required('write')
def server_heartbeat():
    """Receives heartbeat from cfrp_playtime FiveM resource every 30s"""
    data = request.get_json(silent=True) or {}
    players = data.get('players', [])
    now = datetime.now(timezone.utc)

    seen_ids = set()
    for p in players:
        discord_id = p.get('discord_id')
        if not discord_id:
            continue
        seen_ids.add(discord_id)

        hb = PlayerHeartbeat.query.filter_by(discord_id=discord_id).first()
        if not hb:
            # New session
            session = PlayerSession(
                discord_id=discord_id,
                player_name=p.get('name', 'Unknown'),
                join_time=now,
                cash_at_leave=p.get('cash', 0),
                bank_at_leave=p.get('bank', 0),
            )
            db.session.add(session)
            db.session.flush()
            hb = PlayerHeartbeat(
                discord_id=discord_id,
                player_name=p.get('name'),
                last_seen=now,
                session_id=session.id,
                x=p.get('x', 0), y=p.get('y', 0), z=p.get('z', 0),
                cash=p.get('cash', 0), bank=p.get('bank', 0),
                ping=p.get('ping', 0),
            )
            db.session.add(hb)
        else:
            hb.last_seen = now
            hb.player_name = p.get('name', hb.player_name)
            hb.x = p.get('x', 0)
            hb.y = p.get('y', 0)
            hb.z = p.get('z', 0)
            hb.cash = p.get('cash', 0)
            hb.bank = p.get('bank', 0)
            hb.ping = p.get('ping', 0)

    # Mark players who left (not in this heartbeat) as offline
    cutoff = now - timedelta(seconds=90)
    stale = PlayerHeartbeat.query.filter(
        PlayerHeartbeat.last_seen < cutoff
    ).all()
    for hb in stale:
        if hb.session_id:
            session = PlayerSession.query.get(hb.session_id)
            if session and not session.leave_time:
                session.leave_time = hb.last_seen
                join_aware = session.join_time if session.join_time.tzinfo else session.join_time.replace(tzinfo=timezone.utc)
                last_seen_aware = hb.last_seen if hb.last_seen.tzinfo else hb.last_seen.replace(tzinfo=timezone.utc)
                duration = int((last_seen_aware - join_aware).total_seconds())
                session.duration_seconds = max(0, duration)
                _update_playtime_aggregate(hb.discord_id, session.join_time.date(), session.duration_seconds)
        db.session.delete(hb)

    db.session.commit()
    # Run flag detection every ~50 heartbeats (roughly every 25 minutes at 30s intervals)
    import random
    if random.randint(1, 50) == 1:
        try:
            from app.analytics.routes import _run_economy_flag_check
            _run_economy_flag_check()
        except Exception:
            pass
    return jsonify({'success': True, 'tracked': len(players)})


@api_bp.route('/server/disconnect', methods=['POST'])
@api_key_required('write')
def server_disconnect():
    """Receives disconnect event from cfrp_playtime"""
    data = request.get_json(silent=True) or {}
    discord_id = data.get('discord_id')
    if not discord_id:
        return jsonify({'error': 'discord_id required'}), 400

    now = datetime.now(timezone.utc)
    hb = PlayerHeartbeat.query.filter_by(discord_id=discord_id).first()
    if hb and hb.session_id:
        session = PlayerSession.query.get(hb.session_id)
        if session and not session.leave_time:
            session.leave_time = now
            join_aware = session.join_time if session.join_time.tzinfo else session.join_time.replace(tzinfo=timezone.utc)
            now_aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
            duration = int((now_aware - join_aware).total_seconds())
            session.duration_seconds = max(0, duration)
            session.disconnect_reason = data.get('reason', '')
            session.disconnect_category = data.get('category', 'unknown')
            session.last_x = data.get('x')
            session.last_y = data.get('y')
            session.last_z = data.get('z')
            session.disconnect_ping = data.get('ping', 0)
            _update_playtime_aggregate(discord_id, session.join_time.date(), session.duration_seconds)
            # Auto-diagnose crash/timeout sessions immediately
            if session.disconnect_category in ('crash', 'timeout', 'connection'):
                try:
                    from app.analytics.routes import auto_diagnose_session
                    auto_diagnose_session(session)
                except Exception:
                    pass
        db.session.delete(hb)

    db.session.commit()
    return jsonify({'success': True})


@api_bp.route('/server/stats', methods=['POST'])
@api_key_required('write')
def server_stats():
    """Receives kill/death events from cfrp_stats FiveM resource"""
    data = request.get_json(silent=True) or {}
    from app.models import PlayerStat
    stat = PlayerStat(
        discord_id=data.get('discord_id', ''),
        citizenid=data.get('citizenid'),
        event_type=data.get('event_type', 'death'),
        cause=data.get('cause'),
        weapon=data.get('weapon'),
        killer_discord_id=data.get('killer_discord_id'),
        killer_citizenid=data.get('killer_citizenid'),
        killer_name=data.get('killer_name'),
        victim_name=data.get('victim_name'),
        x=data.get('x'), y=data.get('y'), z=data.get('z'),
    )
    db.session.add(stat)
    db.session.commit()
    return jsonify({'success': True})


# ─── Public Analytics API ─────────────────────────────────────────────────────

@api_bp.route('/analytics/live')
@api_key_required('read')
def analytics_live():
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=90)
    players = PlayerHeartbeat.query.filter(PlayerHeartbeat.last_seen >= cutoff).all()
    return jsonify({
        'count': len(players),
        'players': [
            {
                'discord_id': p.discord_id,
                'name': p.player_name,
                'last_seen': p.last_seen.isoformat() if p.last_seen else None,
            }
            for p in players
        ]
    })


@api_bp.route('/analytics/summary')
@api_key_required('read')
def analytics_summary():
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    total_playtime_today = db.session.query(db.func.sum(PlayerSession.duration_seconds))\
        .filter(PlayerSession.join_time >= today).scalar() or 0

    total_playtime_week = db.session.query(db.func.sum(PlayerSession.duration_seconds))\
        .filter(PlayerSession.join_time >= week_ago).scalar() or 0

    kills_week = PlayerStat.query.filter(
        PlayerStat.event_type == 'kill', PlayerStat.recorded_at >= week_ago).count()
    deaths_week = PlayerStat.query.filter(
        PlayerStat.event_type == 'death', PlayerStat.recorded_at >= week_ago).count()

    unique_players = db.session.query(db.func.count(db.func.distinct(PlayerSession.discord_id))).scalar() or 0

    return jsonify({
        'playtime_today_seconds': total_playtime_today,
        'playtime_today_formatted': format_duration(total_playtime_today),
        'playtime_week_seconds': total_playtime_week,
        'playtime_week_formatted': format_duration(total_playtime_week),
        'kills_week': kills_week,
        'deaths_week': deaths_week,
        'unique_players_all_time': unique_players,
    })


@api_bp.route('/analytics/leaderboard')
@api_key_required('read')
def analytics_leaderboard():
    period = request.args.get('period', 'weekly')
    limit = min(request.args.get('limit', 10, type=int), 50)
    now = datetime.now(timezone.utc)

    if period == 'daily':
        cutoff = now - timedelta(days=1)
    elif period == 'monthly':
        cutoff = now - timedelta(days=30)
    elif period == 'yearly':
        cutoff = now - timedelta(days=365)
    else:
        cutoff = now - timedelta(days=7)

    rows = db.session.query(
        PlayerSession.discord_id,
        PlayerSession.player_name,
        db.func.sum(PlayerSession.duration_seconds).label('total_seconds')
    ).filter(
        PlayerSession.join_time >= cutoff,
        PlayerSession.duration_seconds.isnot(None)
    ).group_by(PlayerSession.discord_id, PlayerSession.player_name)\
     .order_by(db.func.sum(PlayerSession.duration_seconds).desc())\
     .limit(limit).all()

    return jsonify({
        'period': period,
        'leaderboard': [
            {
                'rank': i + 1,
                'discord_id': r.discord_id,
                'name': r.player_name,
                'seconds': r.total_seconds,
                'formatted': format_duration(r.total_seconds),
            }
            for i, r in enumerate(rows)
        ]
    })


@api_bp.route('/analytics/killfeed')
@api_key_required('read')
def analytics_killfeed():
    limit = min(request.args.get('limit', 20, type=int), 100)
    kills = PlayerStat.query.filter_by(event_type='kill')\
        .order_by(PlayerStat.recorded_at.desc()).limit(limit).all()
    return jsonify({
        'kills': [
            {
                'killer': k.killer_name,
                'killer_discord': k.discord_id,
                'victim': k.victim_name,
                'victim_discord': k.killer_discord_id,
                'weapon': k.weapon,
                'cause': k.cause,
                'time': k.recorded_at.isoformat(),
                'time_ago': time_ago(k.recorded_at),
            }
            for k in kills
        ]
    })


@api_bp.route('/analytics/crashes')
@api_key_required('read')
def analytics_crashes():
    seven_days = datetime.now(timezone.utc) - timedelta(days=7)
    crashes = PlayerSession.query.filter(
        PlayerSession.join_time >= seven_days,
        PlayerSession.disconnect_category == 'crash'
    ).count()
    timeouts = PlayerSession.query.filter(
        PlayerSession.join_time >= seven_days,
        PlayerSession.disconnect_category == 'timeout'
    ).count()
    network_drops = PlayerSession.query.filter(
        PlayerSession.join_time >= seven_days,
        PlayerSession.disconnect_category == 'connection'
    ).count()

    avg_ping = db.session.query(db.func.avg(PlayerSession.disconnect_ping)).filter(
        PlayerSession.join_time >= seven_days,
        PlayerSession.disconnect_category.in_(['crash', 'timeout']),
        PlayerSession.disconnect_ping.isnot(None)
    ).scalar() or 0

    return jsonify({
        'crashes_7d': crashes,
        'timeouts_7d': timeouts,
        'network_drops_7d': network_drops,
        'avg_ping_at_crash': round(float(avg_ping), 1),
    })


# ─── Applications API ─────────────────────────────────────────────────────────

@api_bp.route('/applications')
@api_key_required('read')
def api_applications():
    page = request.args.get('page', 1, type=int)
    status = request.args.get('status', '')
    type_slug = request.args.get('type', '')

    query = Application.query
    if status:
        query = query.filter_by(status=status)
    if type_slug:
        app_type = ApplicationType.query.filter_by(slug=type_slug).first()
        if app_type:
            query = query.filter_by(type_id=app_type.id)

    pagination = query.order_by(Application.submitted_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return jsonify({
        'applications': [a.to_dict() for a in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'page': page,
    })


@api_bp.route('/applications/<int:app_id>')
@api_key_required('read')
def api_application(app_id):
    application = Application.query.get_or_404(app_id)
    return jsonify(application.to_dict())


@api_bp.route('/applications/<int:app_id>/status', methods=['PATCH'])
@api_key_required('write')
def api_update_application(app_id):
    application = Application.query.get_or_404(app_id)
    data = request.get_json(silent=True) or {}
    new_status = data.get('status')
    allowed = ['pending', 'under_review', 'approved', 'denied', 'on_hold']
    if new_status not in allowed:
        return jsonify({'error': f'Invalid status. Must be one of: {", ".join(allowed)}'}), 400
    application.status = new_status
    application.review_note = data.get('note', application.review_note)
    db.session.commit()
    from app.utils import trigger_webhooks
    action_map = {'approved': 'approve', 'denied': 'deny', 'on_hold': 'hold', 'under_review': 'review'}
    if new_status in action_map:
        trigger_webhooks(application, action_map[new_status])
    return jsonify(application.to_dict())


# ─── Users API ────────────────────────────────────────────────────────────────

@api_bp.route('/users')
@api_key_required('admin')
def api_users():
    page = request.args.get('page', 1, type=int)
    users = User.query.order_by(User.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return jsonify({
        'users': [u.to_dict() for u in users.items],
        'total': users.total,
        'pages': users.pages,
        'page': page,
    })


@api_bp.route('/users/<int:user_id>')
@api_key_required('read')
def api_user(user_id):
    user = User.query.get_or_404(user_id)
    return jsonify(user.to_dict(include_private=True))


@api_bp.route('/users/discord/<discord_id>')
@api_key_required('read')
def api_user_by_discord(discord_id):
    user = User.query.filter_by(discord_id=discord_id).first_or_404()
    return jsonify(user.to_dict())


@api_bp.route('/users/<int:user_id>/sync-discord', methods=['POST'])
@api_key_required('admin')
def api_sync_discord(user_id):
    user = User.query.get_or_404(user_id)
    if not user.discord_id:
        return jsonify({'error': 'User has no Discord connected'}), 400
    from app.discord_oauth.routes import _sync_user_roles
    success = _sync_user_roles(user)
    return jsonify({'success': success})


# ─── Webhook trigger (external) ───────────────────────────────────────────────

@api_bp.route('/webhooks/trigger', methods=['POST'])
@api_key_required('write')
def api_trigger_webhook():
    data = request.get_json(silent=True) or {}
    app_id = data.get('application_id')
    event = data.get('event', 'submit')
    if not app_id:
        return jsonify({'error': 'application_id required'}), 400
    application = Application.query.get_or_404(app_id)
    from app.utils import trigger_webhooks
    trigger_webhooks(application, event)
    return jsonify({'success': True, 'application_id': app_id, 'event': event})


# ─── Roles API ────────────────────────────────────────────────────────────────

@api_bp.route('/roles')
@api_key_required('read')
def api_roles():
    roles = Role.query.order_by(Role.priority.desc()).all()
    return jsonify({'roles': [
        {'id': r.id, 'name': r.name, 'display_name': r.display_name,
         'color': r.color, 'discord_role_id': r.discord_role_id, 'priority': r.priority}
        for r in roles
    ]})


@api_bp.route('/users/<int:user_id>/roles', methods=['POST'])
@api_key_required('admin')
def api_assign_role(user_id):
    data = request.get_json(silent=True) or {}
    user = User.query.get_or_404(user_id)
    role_name = data.get('role')
    action = data.get('action', 'add')  # add or remove
    role = Role.query.filter_by(name=role_name).first()
    if not role:
        return jsonify({'error': 'Role not found'}), 404
    if action == 'add':
        if role not in user.roles:
            user.roles.append(role)
    elif action == 'remove':
        user.roles = [r for r in user.roles if r.id != role.id]
    db.session.commit()
    return jsonify({'success': True, 'user': user.to_dict()})


# ─── Health ───────────────────────────────────────────────────────────────────

@api_bp.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.now(timezone.utc).isoformat()})


@api_bp.route('/docs')
def api_docs():
    return jsonify({
        'version': '1.0',
        'endpoints': {
            'GET /api/health': 'Health check',
            'GET /api/analytics/live': 'Live player count',
            'GET /api/analytics/summary': 'Server summary stats',
            'GET /api/analytics/leaderboard?period=weekly': 'Playtime leaderboard',
            'GET /api/analytics/killfeed': 'Recent kill feed',
            'GET /api/analytics/crashes': 'Crash/disconnect stats',
            'GET /api/applications': 'List applications',
            'GET /api/applications/:id': 'Single application',
            'PATCH /api/applications/:id/status': 'Update application status',
            'GET /api/users': 'List users (admin scope)',
            'GET /api/users/:id': 'Get user',
            'GET /api/users/discord/:discord_id': 'Get user by Discord ID',
            'POST /api/users/:id/sync-discord': 'Sync Discord roles',
            'POST /api/users/:id/roles': 'Assign/remove role',
            'GET /api/roles': 'List roles',
            'POST /api/webhooks/trigger': 'Trigger webhook',
            'POST /api/server/heartbeat': 'FiveM heartbeat (bot key)',
            'POST /api/server/disconnect': 'FiveM disconnect (bot key)',
            'POST /api/server/stats': 'FiveM kill/death (bot key)',
        },
        'authentication': 'X-API-Key header or ?api_key= query param',
        'scopes': ['read', 'write', 'admin'],
    })


# ─── Helper ───────────────────────────────────────────────────────────────────

def _update_playtime_aggregate(discord_id, date, seconds):
    from app.models import PlaytimeAggregate
    agg = PlaytimeAggregate.query.filter_by(discord_id=discord_id, date=date).first()
    if agg:
        agg.seconds = (agg.seconds or 0) + seconds
    else:
        hb = PlayerHeartbeat.query.filter_by(discord_id=discord_id).first()
        name = hb.player_name if hb else 'Unknown'
        agg = PlaytimeAggregate(discord_id=discord_id, player_name=name, date=date, seconds=seconds)
        db.session.add(agg)

# ── Whitelist control ────────────────────────────────────────────────────
from app.api.whitelist_routes import *   # noqa: F401,F403

@api_bp.route('/users/discord/<discord_id>/limits')
@api_key_required('read')
def api_user_limits_by_discord(discord_id):
    """GET /api/users/discord/<discord_id>/limits?active=1"""
    from app.models import UserLimit, User
    from datetime import datetime, timezone

    raw_id = discord_id.lstrip('discord:').strip()

    user = User.query.filter(
        (User.discord_id == raw_id) |
        (User.discord_id == 'discord:' + raw_id)
    ).first()

    if not user:
        return jsonify([])

    active_only = request.args.get('active', '1') == '1'
    query = UserLimit.query.filter_by(user_id=user.id)
    if active_only:
        query = query.filter_by(is_active=True)

    now = datetime.now(timezone.utc)
    limits = []
    for lim in query.order_by(UserLimit.created_at.desc()).all():
        if active_only and lim.expires_at:
            exp = lim.expires_at if lim.expires_at.tzinfo else lim.expires_at.replace(tzinfo=timezone.utc)
            if now > exp:
                continue
        limits.append(lim.to_dict())

    return jsonify(limits)
