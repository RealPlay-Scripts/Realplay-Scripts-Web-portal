import json
import secrets
from datetime import datetime, timezone, timedelta
from flask import (render_template, redirect, url_for, flash, request,
                   jsonify, current_app, abort)
from flask_login import login_required, current_user
from app.admin import admin_bp
from app.models import (db, User, Role, Permission, Application, ApplicationType,
                        Webhook, WebhookDelivery, SiteSettings, AuditLog,
                        Notification, PlayerSession, PlayerHeartbeat)
from app.utils import save_upload, add_discord_role, remove_discord_role


def require_admin(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/')
@login_required
@require_admin
def dashboard():
    stats = {
        'total_users': User.query.count(),
        'active_users': User.query.filter_by(is_active=True).count(),
        'total_applications': Application.query.count(),
        'pending_applications': Application.query.filter_by(status='pending').count(),
        'approved_applications': Application.query.filter_by(status='approved').count(),
        'denied_applications': Application.query.filter_by(status='denied').count(),
        'total_roles': Role.query.count(),
        'application_types': ApplicationType.query.count(),
    }
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=90)
    stats['live_players'] = PlayerHeartbeat.query.filter(PlayerHeartbeat.last_seen >= cutoff).count()
    try:
        from app.models import EconomyFlag
        stats['unreviewed_flags'] = EconomyFlag.query.filter_by(is_reviewed=False).count()
    except Exception:
        stats['unreviewed_flags'] = 0
    try:
        from app.models import Report
        stats['pending_reports'] = Report.query.filter_by(status='pending').count()
    except Exception:
        stats['pending_reports'] = 0
    recent_applications = Application.query.order_by(Application.submitted_at.desc()).limit(8).all()
    recent_users = User.query.order_by(User.created_at.desc()).limit(8).all()
    recent_audit = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(10).all()
    daily_apps = []
    for i in range(7):
        day = datetime.now(timezone.utc) - timedelta(days=6 - i)
        count = Application.query.filter(
            Application.submitted_at >= day.replace(hour=0, minute=0, second=0),
            Application.submitted_at < day.replace(hour=23, minute=59, second=59)
        ).count()
        daily_apps.append({'day': day.strftime('%a'), 'count': count})
    return render_template('admin/dashboard.html', stats=stats,
        recent_applications=recent_applications, recent_users=recent_users,
        recent_audit=recent_audit, daily_apps=daily_apps)


@admin_bp.route('/users')
@login_required
@require_admin
def users():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('q', '')
    role_filter = request.args.get('role', '')
    status_filter = request.args.get('status', '')
    query = User.query
    if search:
        query = query.filter(
            (User.username.ilike(f'%{search}%')) |
            (User.email.ilike(f'%{search}%')) |
            (User.discord_username.ilike(f'%{search}%'))
        )
    if role_filter:
        query = query.join(User.roles).filter(Role.name == role_filter)
    if status_filter == 'banned':
        query = query.filter_by(is_banned=True)
    elif status_filter == 'inactive':
        query = query.filter_by(is_active=False)
    elif status_filter == 'no_discord':
        query = query.filter(User.discord_id.is_(None))
    pagination = query.order_by(User.created_at.desc()).paginate(page=page, per_page=25, error_out=False)
    roles = Role.query.order_by(Role.priority.desc()).all()
    return render_template('admin/users.html', pagination=pagination, users=pagination.items,
        roles=roles, search=search, role_filter=role_filter, status_filter=status_filter)


@admin_bp.route('/users/playtime')
@login_required
@require_admin
def users_playtime():
    """Async endpoint — returns live status + total playtime for a list of discord IDs."""
    ids_param = request.args.get('ids', '')
    discord_ids = [x.strip() for x in ids_param.split(',') if x.strip()]
    if not discord_ids:
        return jsonify({})
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=90)
        live_hbs = PlayerHeartbeat.query.filter(
            PlayerHeartbeat.discord_id.in_(discord_ids),
            PlayerHeartbeat.last_seen >= cutoff
        ).all()
        live_set = {hb.discord_id for hb in live_hbs}

        rows = db.session.query(
            PlayerSession.discord_id,
            db.func.sum(PlayerSession.duration_seconds).label('total')
        ).filter(
            PlayerSession.discord_id.in_(discord_ids),
            PlayerSession.duration_seconds.isnot(None)
        ).group_by(PlayerSession.discord_id).all()
        totals = {row.discord_id: int(row.total or 0) for row in rows}

        result = {}
        for did in discord_ids:
            secs = totals.get(did, 0)
            result[did] = {
                'live':     did in live_set,
                'total_secs': secs,
                'total_h':  secs // 3600,
                'total_m':  (secs % 3600) // 60,
            }
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/users/<int:user_id>')
@login_required
@require_admin
def user_detail(user_id):
    user = User.query.get_or_404(user_id)
    roles = Role.query.order_by(Role.priority.desc()).all()
    applications = Application.query.filter_by(user_id=user_id).order_by(Application.submitted_at.desc()).all()
    audit_logs = AuditLog.query.filter_by(user_id=user_id).order_by(AuditLog.created_at.desc()).limit(20).all()
    return render_template('admin/user_detail.html', user=user, roles=roles,
        applications=applications, audit_logs=audit_logs)


@admin_bp.route('/users/<int:user_id>/update', methods=['POST'])
@login_required
@require_admin
def update_user(user_id):
    user = User.query.get_or_404(user_id)
    action = request.form.get('action')
    if action == 'update_roles':
        role_ids = request.form.getlist('roles', type=int)
        user.roles = Role.query.filter(Role.id.in_(role_ids)).all()
        db.session.commit()
        if user.discord_id:
            from app.discord_oauth.routes import _sync_user_roles
            _sync_user_roles(user)
        AuditLog.log('admin.user.roles_updated', user_id=current_user.id,
            resource_type='user', resource_id=user_id,
            details={'roles': [r.name for r in user.roles]}, ip=request.remote_addr)
        db.session.commit()
        flash(f'Roles updated for {user.username}.', 'success')
    elif action == 'ban':
        reason = request.form.get('reason', 'No reason given')
        user.is_banned = True
        user.ban_reason = reason
        db.session.commit()
        flash(f'{user.username} has been banned.', 'success')
    elif action == 'unban':
        user.is_banned = False
        user.ban_reason = None
        db.session.commit()
        flash(f'{user.username} has been unbanned.', 'success')
    elif action == 'deactivate':
        user.is_active = False
        db.session.commit()
        flash(f'{user.username} deactivated.', 'success')
    elif action == 'activate':
        user.is_active = True
        db.session.commit()
        flash(f'{user.username} activated.', 'success')
    elif action == 'reset_password':
        new_pw = request.form.get('new_password', '')
        if len(new_pw) < 8:
            flash('Password must be at least 8 characters.', 'error')
        else:
            user.set_password(new_pw)
            db.session.commit()
            flash(f'Password reset for {user.username}.', 'success')
    elif action == 'delete':
        if user.id == current_user.id:
            flash('You cannot delete your own account.', 'error')
        else:
            username = user.username
            db.session.delete(user)
            db.session.commit()
            flash(f'User {username} deleted.', 'success')
            return redirect(url_for('admin.users'))
    elif action == 'send_notification':
        title = request.form.get('notif_title', '')
        message = request.form.get('notif_message', '')
        ntype = request.form.get('notif_type', 'info')
        if title and message:
            notif = Notification(user_id=user.id, title=title, message=message, type=ntype)
            db.session.add(notif)
            db.session.commit()
            flash(f'Notification sent to {user.username}.', 'success')
    elif action == 'set_discord_id':
        new_id = request.form.get('discord_id', '').strip()
        if not new_id:
            flash('Discord ID cannot be empty.', 'error')
        elif not new_id.isdigit():
            flash('Discord ID must be numeric.', 'error')
        else:
            # Check if another user already has this discord_id
            conflict = User.query.filter(User.discord_id == new_id, User.id != user_id).first()
            if conflict:
                flash(f'Discord ID {new_id} is already linked to {conflict.username}.', 'error')
            else:
                old_id = user.discord_id
                user.discord_id       = new_id
                user.discord_verified = True
                db.session.commit()
                AuditLog.log('admin.user.discord_id_set', user_id=current_user.id,
                    resource_type='user', resource_id=user_id,
                    details={'old_discord_id': old_id, 'new_discord_id': new_id},
                    ip=request.remote_addr)
                db.session.commit()
                flash(f'Discord ID set to {new_id} for {user.username}.', 'success')
    elif action == 'clear_discord_id':
        old_id = user.discord_id
        user.discord_id            = None
        user.discord_username      = None
        user.discord_avatar        = None
        user.discord_verified      = False
        user.discord_access_token  = None
        user.discord_refresh_token = None
        db.session.commit()
        AuditLog.log('admin.user.discord_id_cleared', user_id=current_user.id,
            resource_type='user', resource_id=user_id,
            details={'old_discord_id': old_id}, ip=request.remote_addr)
        db.session.commit()
        flash(f'Discord unlinked from {user.username}.', 'success')
    return redirect(url_for('admin.user_detail', user_id=user_id))


@admin_bp.route('/roles')
@login_required
@require_admin
def roles():
    roles = Role.query.order_by(Role.priority.desc()).all()
    permissions = Permission.query.order_by(Permission.category, Permission.name).all()
    perm_categories = {}
    for p in permissions:
        perm_categories.setdefault(p.category, []).append(p)
    from app.utils import get_discord_guild_roles
    discord_roles = []
    try:
        discord_roles = get_discord_guild_roles()
    except Exception:
        pass
    return render_template('admin/roles.html', roles=roles, perm_categories=perm_categories, discord_roles=discord_roles)


@admin_bp.route('/roles/create', methods=['POST'])
@login_required
@require_admin
def create_role():
    name = request.form.get('name', '').strip().lower().replace(' ', '_')
    display_name = request.form.get('display_name', '').strip()
    if not name or not display_name:
        flash('Name and display name required.', 'error')
        return redirect(url_for('admin.roles'))
    if Role.query.filter_by(name=name).first():
        flash('A role with that name already exists.', 'error')
        return redirect(url_for('admin.roles'))
    role = Role(
        name=name, display_name=display_name,
        description=request.form.get('description', '').strip(),
        color=request.form.get('color', '#6366f1'),
        discord_role_id=request.form.get('discord_role_id', '').strip() or None,
        priority=request.form.get('priority', 0, type=int),
    )
    perm_ids = request.form.getlist('permissions', type=int)
    role.permissions = Permission.query.filter(Permission.id.in_(perm_ids)).all()
    db.session.add(role)
    db.session.commit()
    flash(f'Role "{display_name}" created.', 'success')
    return redirect(url_for('admin.roles'))


@admin_bp.route('/roles/<int:role_id>/update', methods=['POST'])
@login_required
@require_admin
def update_role(role_id):
    role = Role.query.get_or_404(role_id)
    role.display_name = request.form.get('display_name', role.display_name).strip()
    role.description = request.form.get('description', '').strip()
    role.color = request.form.get('color', role.color)
    role.discord_role_id = request.form.get('discord_role_id', '').strip() or None
    role.priority = request.form.get('priority', role.priority, type=int)
    perm_ids = request.form.getlist('permissions', type=int)
    role.permissions = Permission.query.filter(Permission.id.in_(perm_ids)).all()
    db.session.commit()
    flash(f'Role "{role.display_name}" updated.', 'success')
    return redirect(url_for('admin.roles'))


@admin_bp.route('/roles/<int:role_id>/delete', methods=['POST'])
@login_required
@require_admin
def delete_role(role_id):
    role = Role.query.get_or_404(role_id)
    if role.is_system:
        flash('Cannot delete system roles.', 'error')
        return redirect(url_for('admin.roles'))
    name = role.display_name
    db.session.delete(role)
    db.session.commit()
    flash(f'Role "{name}" deleted.', 'success')
    return redirect(url_for('admin.roles'))


@admin_bp.route('/applications')
@login_required
@require_admin
def application_types():
    types = ApplicationType.query.order_by(ApplicationType.sort_order, ApplicationType.created_at).all()
    for t in types:
        t.pending_count = Application.query.filter_by(type_id=t.id, status='pending').count()
        t.total_count = Application.query.filter_by(type_id=t.id).count()
    return render_template('admin/application_types.html', types=types)


@admin_bp.route('/applications/create', methods=['GET', 'POST'])
@login_required
@require_admin
def create_application_type():
    if request.method == 'POST':
        from slugify import slugify
        name = request.form.get('name', '').strip()
        slug = slugify(name)
        base_slug = slug
        i = 1
        while ApplicationType.query.filter_by(slug=slug).first():
            slug = f'{base_slug}-{i}'
            i += 1
        app_type = ApplicationType(
            name=name, slug=slug,
            description=request.form.get('description', ''),
            icon=request.form.get('icon', 'file-text'),
            color=request.form.get('color', '#6366f1'),
            is_active=request.form.get('is_active') == 'on',
            requires_discord=request.form.get('requires_discord') == 'on',
            max_applications=request.form.get('max_applications', 1, type=int),
            cooldown_days=request.form.get('cooldown_days', 30, type=int),
            auto_approve=request.form.get('auto_approve') == 'on',
            discord_roles_on_approve=json.dumps([r for r in request.form.getlist('roles_on_approve') if r]),
            discord_roles_on_deny=json.dumps([r for r in request.form.getlist('roles_on_deny') if r]),
            discord_roles_on_submit=json.dumps([r for r in request.form.getlist('roles_on_submit') if r]),
            discord_roles_remove_on_submit=json.dumps([r for r in request.form.getlist('roles_remove_on_submit') if r]),
            discord_roles_remove_on_approve=json.dumps([r for r in request.form.getlist('roles_remove_on_approve') if r]),
            discord_roles_remove_on_deny=json.dumps([r for r in request.form.getlist('roles_remove_on_deny') if r]),
            
            sort_order=request.form.get('sort_order', 0, type=int),
            created_by=current_user.id,
        )
        schema_json = request.form.get('form_schema', '[]')
        try:
            json.loads(schema_json)
            app_type.form_schema = schema_json
        except Exception:
            app_type.form_schema = '[]'
        role_ids = request.form.getlist('visible_roles', type=int)
        app_type.visible_to_roles = Role.query.filter(Role.id.in_(role_ids)).all()
        db.session.add(app_type)
        db.session.commit()
        flash(f'Application type "{name}" created.', 'success')
        return redirect(url_for('admin.edit_application_type', type_id=app_type.id))
    roles = Role.query.order_by(Role.priority.desc()).all()
    from app.utils import get_discord_guild_roles
    discord_roles = []
    try:
        discord_roles = get_discord_guild_roles()
    except Exception:
        pass
    return render_template('admin/application_type_edit.html', app_type=None, roles=roles, discord_roles=discord_roles)


@admin_bp.route('/applications/<int:type_id>/edit', methods=['GET', 'POST'])
@login_required
@require_admin
def edit_application_type(type_id):
    app_type = ApplicationType.query.get_or_404(type_id)
    if request.method == 'POST':
        app_type.name = request.form.get('name', app_type.name).strip()
        app_type.description = request.form.get('description', '')
        app_type.icon = request.form.get('icon', 'file-text')
        app_type.color = request.form.get('color', '#6366f1')
        app_type.is_active = request.form.get('is_active') == 'on'
        app_type.requires_discord = request.form.get('requires_discord') == 'on'
        app_type.max_applications = request.form.get('max_applications', 1, type=int)
        app_type.cooldown_days = request.form.get('cooldown_days', 30, type=int)
        app_type.auto_approve = request.form.get('auto_approve') == 'on'
        app_type.discord_roles_on_approve=json.dumps([r for r in request.form.getlist('roles_on_approve') if r]); app_type.discord_roles_on_deny=json.dumps([r for r in request.form.getlist('roles_on_deny') if r]); app_type.discord_roles_on_submit=json.dumps([r for r in request.form.getlist('roles_on_submit') if r]); app_type.discord_roles_remove_on_submit=json.dumps([r for r in request.form.getlist('roles_remove_on_submit') if r]); app_type.discord_roles_remove_on_approve=json.dumps([r for r in request.form.getlist('roles_remove_on_approve') if r]); app_type.discord_roles_remove_on_deny=json.dumps([r for r in request.form.getlist('roles_remove_on_deny') if r])
        
        app_type.sort_order = request.form.get('sort_order', 0, type=int)
        schema_json = request.form.get('form_schema', '[]')
        try:
            json.loads(schema_json)
            app_type.form_schema = schema_json
        except Exception:
            pass
        role_ids = request.form.getlist('visible_roles', type=int)
        app_type.visible_to_roles = Role.query.filter(Role.id.in_(role_ids)).all()
        db.session.commit()
        flash('Application type updated.', 'success')
        return redirect(url_for('admin.edit_application_type', type_id=type_id))
    roles = Role.query.order_by(Role.priority.desc()).all()
    applications = Application.query.filter_by(type_id=type_id).order_by(Application.submitted_at.desc()).limit(20).all()
    from app.utils import get_discord_guild_roles
    discord_roles = []
    try:
        discord_roles = get_discord_guild_roles()
    except Exception:
        pass
    webhooks = Webhook.query.filter_by(type_id=type_id).all()
    return render_template('admin/application_type_edit.html', app_type=app_type, roles=roles,
        applications=applications, discord_roles=discord_roles, webhooks=webhooks)


@admin_bp.route('/applications/<int:type_id>/delete', methods=['POST'])
@login_required
@require_admin
def delete_application_type(type_id):
    app_type = ApplicationType.query.get_or_404(type_id)
    name = app_type.name
    db.session.delete(app_type)
    db.session.commit()
    flash(f'Application type "{name}" deleted.', 'success')
    return redirect(url_for('admin.application_types'))


@admin_bp.route('/reviews')
@login_required
@require_admin
def reviews():
    page = request.args.get('page', 1, type=int)
    status = request.args.get('status', 'pending')
    type_id = request.args.get('type_id', None, type=int)
    search = request.args.get('q', '')
    query = Application.query
    if status and status != 'all':
        query = query.filter_by(status=status)
    if type_id:
        query = query.filter_by(type_id=type_id)
    if search:
        query = query.join(User).filter(
            (User.username.ilike(f'%{search}%')) |
            (User.discord_username.ilike(f'%{search}%'))
        )
    pagination = query.order_by(Application.submitted_at.desc()).paginate(page=page, per_page=20, error_out=False)
    app_types = ApplicationType.query.all()
    return render_template('admin/reviews.html', pagination=pagination, applications=pagination.items,
        app_types=app_types, current_status=status, current_type=type_id, search=search)


@admin_bp.route('/reviews/<int:app_id>', methods=['GET', 'POST'])
@login_required
@require_admin
def review_application(app_id):
    application = Application.query.get_or_404(app_id)
    if request.method == 'POST':
        action = request.form.get('action')
        note = request.form.get('note', '').strip()
        old_status = application.status
        if action in ('approve', 'deny', 'hold', 'review', 'pending'):
            status_map = {'approve': 'approved', 'deny': 'denied', 'hold': 'on_hold',
                          'review': 'under_review', 'pending': 'pending'}
            application.status = status_map[action]
            application.reviewed_by = current_user.id
            application.reviewed_at = datetime.now(timezone.utc)
            application.review_note = note
            db.session.commit()
            user = application.applicant
            app_type = application.application_type
            if user.discord_id:
                if action == 'approve':
                    for rid in app_type.get_roles_on_approve():
                        add_discord_role(user.discord_id, rid)
                    for rid in app_type.get_roles_remove_on_approve():
                        remove_discord_role(user.discord_id, rid)
                elif action == 'deny':
                    for rid in app_type.get_roles_on_deny():
                        add_discord_role(user.discord_id, rid)
                    for rid in app_type.get_roles_remove_on_deny():
                        remove_discord_role(user.discord_id, rid)
            notif_msgs = {
                'approved': ('Application Approved ✅', f'Your {app_type.name} application has been approved!', 'success'),
                'denied': ('Application Denied ❌', f'Your {app_type.name} application was not approved.', 'error'),
                'on_hold': ('Application On Hold ⏸️', f'Your {app_type.name} application is on hold.', 'warning'),
                'under_review': ('Under Review 🔍', f'Your {app_type.name} application is being reviewed.', 'info'),
            }
            if application.status in notif_msgs:
                title, msg, ntype = notif_msgs[application.status]
                notif = Notification(user_id=user.id, title=title, message=msg, type=ntype,
                    link=url_for('applications.view_application', app_id=app_id))
                db.session.add(notif)
            from app.utils import trigger_webhooks
            trigger_webhooks(application, action)
            AuditLog.log(f'admin.application.{action}', user_id=current_user.id,
                resource_type='application', resource_id=app_id,
                details={'note': note, 'old_status': old_status}, ip=request.remote_addr)
            db.session.commit()
            flash(f'Application {action}d.', 'success')
        elif action == 'comment':
            from app.models import ApplicationComment
            content = request.form.get('content', '').strip()
            is_internal = request.form.get('is_internal') == 'on'
            if content:
                comment = ApplicationComment(application_id=app_id, user_id=current_user.id,
                    content=content, is_internal=is_internal)
                db.session.add(comment)
                db.session.commit()
                flash('Comment added.', 'success')
        elif action == 'delete':
            applicant_name = application.applicant.username
            app_type_name  = application.application_type.name
            db.session.delete(application)
            db.session.commit()
            AuditLog.log(f'admin.application.delete', user_id=current_user.id,
                resource_type='application', resource_id=app_id,
                details={'applicant': applicant_name, 'type': app_type_name},
                ip=request.remote_addr)
            db.session.commit()
            flash(f'Application #{app_id} deleted.', 'success')
            return redirect(url_for('admin.reviews'))
        return redirect(url_for('admin.review_application', app_id=app_id))
    return render_template('admin/review_application.html', application=application)


@admin_bp.route('/webhooks')
@login_required
@require_admin
def webhooks():
    webhooks = Webhook.query.order_by(Webhook.created_at.desc()).all()
    app_types = ApplicationType.query.all()
    return render_template('admin/webhooks.html', webhooks=webhooks, app_types=app_types)


@admin_bp.route('/webhooks/create', methods=['POST'])
@login_required
@require_admin
def create_webhook():
    name = request.form.get('name', '').strip()
    url = request.form.get('url', '').strip()
    type_id = request.form.get('type_id', type=int)
    if not name or not url:
        flash('Name and URL are required.', 'error')
        return redirect(url_for('admin.webhooks'))
    embed_config = {
        'username': request.form.get('embed_username', 'CFRP Whitelist'),
        'avatar_url': request.form.get('embed_avatar', ''),
        'color': int(request.form.get('embed_color', '6366f1').lstrip('#'), 16),
    }
    wh = Webhook(
        name=name, url=url, type_id=type_id if type_id else None,
        secret=secrets.token_hex(16),
        on_submit=request.form.get('on_submit') == 'on',
        on_approve=request.form.get('on_approve') == 'on',
        on_deny=request.form.get('on_deny') == 'on',
        on_review=request.form.get('on_review') == 'on',
        on_hold=request.form.get('on_hold') == 'on',
        embed_config=json.dumps(embed_config),
        created_by=current_user.id,
    )
    db.session.add(wh)
    db.session.commit()
    flash(f'Webhook "{name}" created.', 'success')
    return redirect(url_for('admin.webhooks'))


@admin_bp.route('/webhooks/<int:wh_id>/test', methods=['POST'])
@login_required
@require_admin
def test_webhook(wh_id):
    import requests as req_lib
    wh = Webhook.query.get_or_404(wh_id)
    embed_cfg = wh.get_embed_config()
    payload = {
        'username': embed_cfg.get('username', 'CFRP Whitelist'),
        'embeds': [{'title': '🔔 Webhook Test',
            'description': f'Test from **{current_user.username}**.',
            'color': embed_cfg.get('color', 6316281),
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'footer': {'text': 'CFRP Whitelist — Webhook Test'}}]
    }
    try:
        resp = req_lib.post(wh.url, json=payload, timeout=10)
        return jsonify({'success': resp.status_code in (200, 204), 'status': resp.status_code})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@admin_bp.route('/webhooks/<int:wh_id>/toggle', methods=['POST'])
@login_required
@require_admin
def toggle_webhook(wh_id):
    wh = Webhook.query.get_or_404(wh_id)
    wh.is_active = not wh.is_active
    db.session.commit()
    return jsonify({'success': True, 'active': wh.is_active})


@admin_bp.route('/webhooks/<int:wh_id>/delete', methods=['POST'])
@login_required
@require_admin
def delete_webhook(wh_id):
    wh = Webhook.query.get_or_404(wh_id)
    name = wh.name
    db.session.delete(wh)
    db.session.commit()
    flash(f'Webhook "{name}" deleted.', 'success')
    return redirect(url_for('admin.webhooks'))


@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
@require_admin
def settings():
    if request.method == 'POST':
        keys = ['site_name', 'site_tagline', 'primary_color', 'discord_invite',
                'fivem_server_ip', 'maintenance_mode', 'allow_registration', 'require_discord']
        for key in keys:
            existing = SiteSettings.query.filter_by(key=key).first()
            if not existing:
                continue
            if existing.value_type == 'bool':
                existing.value = 'true' if request.form.get(key) == 'on' else 'false'
            else:
                val = request.form.get(key)
                if val is not None:
                    existing.value = val
        db.session.commit()
        if 'logo_file' in request.files:
            file = request.files['logo_file']
            if file and file.filename:
                path = save_upload(file, 'branding', 'logo_')
                if path:
                    s = SiteSettings.query.filter_by(key='site_logo').first()
                    if s:
                        s.value = path
                    db.session.commit()
        flash('Settings saved.', 'success')
        return redirect(url_for('admin.settings'))
    all_settings = {s.key: s for s in SiteSettings.query.all()}
    return render_template('admin/settings.html', settings=all_settings)


@admin_bp.route('/audit')
@login_required
@require_admin
def audit_log():
    page = request.args.get('page', 1, type=int)
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template('admin/audit.html', pagination=logs, logs=logs.items)

# ── Whitelist admin ───────────────────────────────────────────────────────
from datetime import datetime, timezone
from app.models import WhitelistSchedule

@admin_bp.route('/whitelist')
@login_required
@require_admin
def whitelist():
    enabled = SiteSettings.get('whitelist_enabled', True)
    schedules = WhitelistSchedule.query.order_by(WhitelistSchedule.scheduled_at.asc()).all()
    return render_template('admin/whitelist.html', whitelist_enabled=enabled, schedules=schedules)

@admin_bp.route('/whitelist/toggle', methods=['POST'])
@login_required
@require_admin
def whitelist_toggle():
    current = SiteSettings.get('whitelist_enabled', True)
    new_state = not current
    SiteSettings.set('whitelist_enabled', new_state, 'bool', 'FiveM whitelist active', 'whitelist')
    log = AuditLog(user_id=current_user.id, action='whitelist.set_' + ('on' if new_state else 'off'),
                   resource_type='setting', resource_id='whitelist_enabled')
    db.session.add(log)
    db.session.commit()
    return jsonify({'enabled': new_state})

@admin_bp.route('/whitelist/schedule', methods=['POST'])
@login_required
@require_admin
def whitelist_create_schedule():
    data = request.get_json(silent=True) or {}
    try:
        dt_str = data.get('scheduled_at', '')
        dt = datetime.fromisoformat(dt_str.replace('Z', ''))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return jsonify({'error': 'Invalid scheduled_at'}), 400
    repeat = data.get('repeat_type', 'once')
    if repeat not in ('once', 'daily', 'weekly'):
        return jsonify({'error': 'Invalid repeat_type'}), 400
    s = WhitelistSchedule(label=data.get('label', ''), enabled=bool(data.get('enabled', True)),
                          scheduled_at=dt, repeat_type=repeat, created_by=current_user.id)
    db.session.add(s)
    db.session.commit()
    return jsonify(s.to_dict()), 201

@admin_bp.route('/whitelist/schedule/<int:schedule_id>', methods=['DELETE'])
@login_required
@require_admin
def whitelist_delete_schedule(schedule_id):
    s = WhitelistSchedule.query.get_or_404(schedule_id)
    db.session.delete(s)
    db.session.commit()
    return jsonify({'deleted': schedule_id})


# ─── Live User Data ────────────────────────────────────────────────────────────

@admin_bp.route('/users/<int:user_id>/live')
@login_required
@require_admin
def user_live_data(user_id):
    """Returns live playtime and in-game location for a single user."""
    user = User.query.get_or_404(user_id)
    if not user.discord_id:
        return jsonify({'online': False, 'error': 'No Discord ID linked'})

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=90)
        hb = PlayerHeartbeat.query.filter(
            PlayerHeartbeat.discord_id == user.discord_id,
            PlayerHeartbeat.last_seen >= cutoff
        ).first()

        # Total all-time playtime
        total_row = db.session.query(
            db.func.sum(PlayerSession.duration_seconds).label('total')
        ).filter(
            PlayerSession.discord_id == user.discord_id,
            PlayerSession.duration_seconds.isnot(None)
        ).first()
        total_secs = int(total_row.total or 0) if total_row else 0

        # Today's playtime (from aggregates + live session)
        today = datetime.now(timezone.utc).date()
        from app.models import PlaytimeAggregate
        today_row = PlaytimeAggregate.query.filter_by(
            discord_id=user.discord_id, date=today
        ).first()
        today_secs = today_row.seconds if today_row else 0

        # If live session running, add live portion
        live_session_secs = 0
        if hb and hb.session_id:
            session = PlayerSession.query.get(hb.session_id)
            if session and not session.leave_time:
                diff = datetime.now(timezone.utc) - session.join_time.replace(tzinfo=timezone.utc)
                live_session_secs = int(diff.total_seconds())
                total_secs += live_session_secs
                today_secs += live_session_secs

        if hb:
            return jsonify({
                'online': True,
                'player_name': hb.player_name,
                'x': hb.x,
                'y': hb.y,
                'z': hb.z,
                'cash': hb.cash,
                'bank': hb.bank,
                'ping': hb.ping,
                'last_seen': hb.last_seen.isoformat(),
                'session_seconds': live_session_secs,
                'session_h': live_session_secs // 3600,
                'session_m': (live_session_secs % 3600) // 60,
                'session_s': live_session_secs % 60,
                'total_secs': total_secs,
                'total_h': total_secs // 3600,
                'total_m': (total_secs % 3600) // 60,
                'today_secs': today_secs,
                'today_h': today_secs // 3600,
                'today_m': (today_secs % 3600) // 60,
            })
        else:
            return jsonify({
                'online': False,
                'total_secs': total_secs,
                'total_h': total_secs // 3600,
                'total_m': (total_secs % 3600) // 60,
                'today_secs': today_secs,
                'today_h': today_secs // 3600,
                'today_m': (today_secs % 3600) // 60,
            })
    except Exception as e:
        return jsonify({'error': str(e), 'online': False}), 500


# ─── Update Log ────────────────────────────────────────────────────────────────

from app.models import UpdateLog

CHANGELOG_WEBHOOK_KEY = 'changelog_webhook_url'

@admin_bp.route('/updates')
@login_required
@require_admin
def update_log():
    entries = UpdateLog.query.order_by(UpdateLog.created_at.desc()).all()
    saved_webhook = SiteSettings.get(CHANGELOG_WEBHOOK_KEY, '')
    return render_template('admin/update_log.html', entries=entries, saved_webhook=saved_webhook)


@admin_bp.route('/updates/save-webhook', methods=['POST'])
@login_required
@require_admin
def update_log_save_webhook():
    url = request.form.get('webhook_url', '').strip()
    s = SiteSettings.query.filter_by(key=CHANGELOG_WEBHOOK_KEY).first()
    if s:
        s.value = url
    else:
        s = SiteSettings(key=CHANGELOG_WEBHOOK_KEY, value=url,
                         value_type='string', description='Update log Discord webhook URL',
                         category='integrations')
        db.session.add(s)
    db.session.commit()
    return jsonify({'ok': True})


@admin_bp.route('/updates/create', methods=['POST'])
@login_required
@require_admin
def update_log_create():
    import json as _j
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'Title required'}), 400

    entry = UpdateLog(
        version=data.get('version', '').strip() or None,
        title=title,
        summary=data.get('summary', '').strip() or None,
        category=data.get('category', 'update'),
        changes=_j.dumps(data.get('changes', [])),
        created_by=current_user.id,
    )
    db.session.add(entry)
    db.session.commit()

    # Optionally post to Discord immediately
    if data.get('post_discord'):
        webhook_url = (data.get('webhook_url') or SiteSettings.get(CHANGELOG_WEBHOOK_KEY, '')).strip()
        if webhook_url:
            _post_update_to_discord(entry, webhook_url)
            db.session.commit()

    AuditLog.log('admin.update_log.created', user_id=current_user.id,
                 details=f'Created update log: {title}')
    return jsonify({'ok': True, 'id': entry.id,
                    'discord_sent': entry.discord_sent})


@admin_bp.route('/updates/<int:entry_id>/edit', methods=['POST'])
@login_required
@require_admin
def update_log_edit(entry_id):
    import json as _j
    entry = UpdateLog.query.get_or_404(entry_id)
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'Title required'}), 400

    entry.title   = title
    entry.version = data.get('version', '').strip() or None
    entry.summary = data.get('summary', '').strip() or None
    entry.category = data.get('category', entry.category)
    entry.changes = _j.dumps(data.get('changes', []))
    db.session.commit()

    # Optionally re-post to Discord after edit
    if data.get('post_discord'):
        webhook_url = (data.get('webhook_url') or SiteSettings.get(CHANGELOG_WEBHOOK_KEY, '')).strip()
        if webhook_url:
            _post_update_to_discord(entry, webhook_url)
            db.session.commit()

    AuditLog.log('admin.update_log.edited', user_id=current_user.id,
                 details=f'Edited update log #{entry_id}: {title}')
    return jsonify({'ok': True, 'discord_sent': entry.discord_sent})


@admin_bp.route('/updates/<int:entry_id>/delete', methods=['POST'])
@login_required
@require_admin
def update_log_delete(entry_id):
    entry = UpdateLog.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    AuditLog.log('admin.update_log.deleted', user_id=current_user.id,
                 details=f'Deleted update log #{entry_id}')
    return jsonify({'ok': True})


@admin_bp.route('/updates/<int:entry_id>/post-discord', methods=['POST'])
@login_required
@require_admin
def update_log_post_discord(entry_id):
    entry = UpdateLog.query.get_or_404(entry_id)
    data = request.get_json(silent=True) or {}
    webhook_url = (data.get('webhook_url') or SiteSettings.get(CHANGELOG_WEBHOOK_KEY, '')).strip()
    if not webhook_url:
        return jsonify({'error': 'No webhook URL configured'}), 400
    ok = _post_update_to_discord(entry, webhook_url)
    db.session.commit()
    return jsonify({'ok': ok, 'discord_sent': entry.discord_sent})


def _post_update_to_discord(entry, webhook_url):
    import requests as _req
    import json as _j

    CATEGORY_META = {
        'update':       {'emoji': '🚀', 'label': 'Server Update',       'color': 0x6366f1},
        'hotfix':       {'emoji': '🔧', 'label': 'Hotfix',              'color': 0xf59e0b},
        'announcement': {'emoji': '📢', 'label': 'Announcement',        'color': 0x3b82f6},
        'maintenance':  {'emoji': '🛠️', 'label': 'Maintenance',         'color': 0x64748b},
        'event':        {'emoji': '🎉', 'label': 'Event',               'color': 0xec4899},
    }
    meta = CATEGORY_META.get(entry.category, CATEGORY_META['update'])

    changes = entry.get_changes()
    TYPE_META = {
        'added':   ('✅', 'Added'),
        'changed': ('🔄', 'Changed'),
        'fixed':   ('🛠️', 'Fixed'),
        'removed': ('❌', 'Removed'),
        'note':    ('📝', 'Notes'),
    }

    # Group changes by type
    grouped = {}
    for c in changes:
        t = c.get('type', 'note')
        grouped.setdefault(t, []).append(c.get('text', '').strip())

    fields = []
    for ctype in ['added', 'changed', 'fixed', 'removed', 'note']:
        items = grouped.get(ctype)
        if not items:
            continue
        ico, label = TYPE_META[ctype]
        value = '\n'.join(f'{ico} {t}' for t in items if t)
        if value:
            fields.append({'name': label, 'value': value[:1024], 'inline': False})

    title_str = f"{meta['emoji']}  {entry.title}"
    if entry.version:
        title_str = f"{meta['emoji']}  {entry.title}  `v{entry.version}`"

    embed = {
        'title': title_str,
        'color': meta['color'],
        'fields': fields,
        'timestamp': entry.created_at.isoformat(),
        'footer': {
            'text': f"{meta['label']} • Posted by {entry.author.username if entry.author else 'Admin'}",
        },
    }
    if entry.summary:
        embed['description'] = f"*{entry.summary}*"

    payload = {
        'username': 'Server Updates',
        'avatar_url': 'https://i.imgur.com/4M34hi2.png',
        'embeds': [embed],
    }

    try:
        resp = _req.post(webhook_url, json=payload, timeout=10)
        entry.discord_sent = resp.status_code in (200, 204)
        entry.discord_sent_at = datetime.now(timezone.utc)
        entry.webhook_url = webhook_url
        return entry.discord_sent
    except Exception as e:
        entry.discord_sent = False
        return False


# ─── In-Game Prems Admin Page ────────────────────────────────────────────────

@admin_bp.route('/ingame-prems')
@login_required
def ingame_prems():
    if not current_user.has_permission('admin.access'):
        abort(403)
    return render_template('admin/ingame_prems.html')
