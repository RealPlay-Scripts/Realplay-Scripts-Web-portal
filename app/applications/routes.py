from datetime import datetime, timezone, timedelta
from flask import (render_template, redirect, url_for, flash, request,
                   jsonify, abort, current_app)
from flask_login import login_required, current_user
from app.applications import applications_bp
from app.models import db, Application, ApplicationType, ApplicationComment, Notification, AuditLog, UserLimit, DefaultLimit
from app.utils import trigger_webhooks, send_discord_dm


# ── Helpers ───────────────────────────────────────────────────────────────────

AGE_FIELD_KEYS   = {'age'}
AGE_FIELD_LABELS = {'how old are you?', 'how old are you', 'age', 'your age'}


def _detect_age_from_responses(schema, responses):
    """Return submitted age as int, or None if not found / not numeric."""
    for field in schema:
        key   = field.get('key', '')
        label = field.get('label', '').lower()
        if key in AGE_FIELD_KEYS or label in AGE_FIELD_LABELS:
            try:
                return int(responses.get(key, ''))
            except (ValueError, TypeError):
                return None
    return None


def _apply_under18_limits(user, default_tmpl=None):
    """Create a UserLimit record for an under-18 user based on the DefaultLimit template."""
    if default_tmpl is None:
        default_tmpl = DefaultLimit.query.filter_by(slug='under_18', is_active=True).first()

    if default_tmpl:
        no_firearm         = default_tmpl.no_firearm
        no_create_priority = default_tmpl.no_create_priority
        no_join_priority   = default_tmpl.no_join_priority
        duration_days      = default_tmpl.duration_days or 90
        notes              = default_tmpl.notes
        label              = default_tmpl.label or 'Under 18 Restriction'
    else:
        # Hard-coded fallback so restrictions always apply even without a DB record
        no_firearm         = True
        no_create_priority = True
        no_join_priority   = True
        duration_days      = 90
        notes              = None
        label              = 'Under 18 Restriction'

    expires_at = datetime.now(timezone.utc) + timedelta(days=duration_days)

    limit = UserLimit(
        user_id            = user.id,
        label              = label,
        no_firearm         = no_firearm,
        no_create_priority = no_create_priority,
        no_join_priority   = no_join_priority,
        notes              = notes,
        expires_at         = expires_at,
        source             = 'under_18',
        is_active          = True,
    )
    db.session.add(limit)
    db.session.flush()   # get limit.id before commit
    return limit


def _build_restriction_dm(limit, is_new=True):
    """Build the Discord DM embed payload for restriction notifications."""
    action_str = 'applied' if is_new else 'updated'
    expires_str = (
        limit.expires_at.strftime('%Y-%m-%d') if limit.expires_at else 'permanent'
    )
    restrictions = limit.active_restrictions()
    restr_text = '\n'.join(f'• {r}' for r in restrictions) if restrictions else 'None'

    embed = {
        'title': f'🚨 CFRP Restriction {action_str.title()}',
        'color': 0xef4444,
        'description': (
            f'A restriction has been **{action_str}** on your account: **{limit.label}**'
        ),
        'fields': [
            {
                'name': 'Active Restrictions',
                'value': restr_text,
                'inline': False,
            },
            {
                'name': 'Expires',
                'value': expires_str,
                'inline': True,
            },
            {
                'name': 'Duration',
                'value': (
                    f'{(limit.expires_at - datetime.now(timezone.utc)).days} days remaining'
                    if limit.expires_at else 'Permanent'
                ),
                'inline': True,
            },
        ],
        'footer': {'text': 'CFRP — If you have questions, contact staff in Discord.'},
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    return embed


# ── Routes ────────────────────────────────────────────────────────────────────

@applications_bp.route('/')
def list_types():
    types = ApplicationType.query.filter_by(is_active=True).order_by(ApplicationType.sort_order).all()
    user_apps = {}
    if current_user.is_authenticated:
        for t in types:
            user_apps[t.id] = Application.query.filter_by(
                user_id=current_user.id, type_id=t.id
            ).order_by(Application.submitted_at.desc()).first()
    return render_template('applications/list.html', types=types, user_apps=user_apps)


@applications_bp.route('/<slug>/apply', methods=['GET', 'POST'])
@login_required
def apply(slug):
    app_type = ApplicationType.query.filter_by(slug=slug, is_active=True).first_or_404()

    # Require Discord
    if app_type.requires_discord and not current_user.discord_id:
        flash('You must connect your Discord account before applying.', 'warning')
        return redirect(url_for('discord_oauth.connect'))

    # Check existing
    existing = Application.query.filter_by(
        user_id=current_user.id, type_id=app_type.id
    ).filter(Application.status.in_(['pending', 'under_review', 'approved', 'on_hold'])).first()
    if existing:
        flash('You already have an active application for this type.', 'warning')
        return redirect(url_for('applications.view_application', app_id=existing.id))

    # Cooldown check
    if app_type.cooldown_days > 0:
        cooldown_cutoff = datetime.now(timezone.utc) - timedelta(days=app_type.cooldown_days)
        recent_denied = Application.query.filter_by(
            user_id=current_user.id, type_id=app_type.id, status='denied'
        ).filter(Application.submitted_at >= cooldown_cutoff).first()
        if recent_denied:
            days_left = app_type.cooldown_days - (datetime.now(timezone.utc) - recent_denied.submitted_at.replace(tzinfo=timezone.utc)).days
            flash(f'You must wait {days_left} more day(s) before reapplying.', 'warning')
            return redirect(url_for('applications.list_types'))

    schema = app_type.get_form_schema()

    if request.method == 'POST':
        # Validate required fields
        responses = {}
        errors = []
        for field in schema:
            key = field.get('key', '')
            value = request.form.get(key, '').strip()
            if field.get('required') and not value:
                errors.append(f'"{field.get("label", key)}" is required.')
            if field.get('min_length') and len(value) < field['min_length']:
                errors.append(f'"{field.get("label", key)}" must be at least {field["min_length"]} characters.')
            responses[key] = value

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('applications/apply.html', app_type=app_type, schema=schema)

        # ── Age detection ──────────────────────────────────────────────────────
        submitted_age = _detect_age_from_responses(schema, responses)
        is_under_18   = submitted_age is not None and submitted_age < 18

        application = Application(
            user_id=current_user.id,
            type_id=app_type.id,
            status='approved' if app_type.auto_approve else 'pending',
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent', '')[:512],
        )
        application.set_responses(responses)
        db.session.add(application)
        db.session.flush()  # get application.id before limit creation

        # ── Under-18 restriction logic ─────────────────────────────────────────
        new_limit = None
        if is_under_18:
            new_limit = _apply_under18_limits(current_user)

        db.session.commit()

        # ── Auto-approve logic ─────────────────────────────────────────────────
        if app_type.auto_approve:
            application.reviewed_at = datetime.now(timezone.utc)
            if current_user.discord_id:
                from app.utils import add_discord_role, remove_discord_role
                for rid in app_type.get_roles_on_approve():
                    add_discord_role(current_user.discord_id, rid)
                for rid in app_type.get_roles_remove_on_approve():
                    remove_discord_role(current_user.discord_id, rid)
            db.session.commit()
            flash('Your application has been automatically approved!', 'success')
        else:
            # Apply on-submit roles
            if current_user.discord_id:
                from app.utils import add_discord_role, remove_discord_role
                for rid in app_type.get_roles_on_submit():
                    add_discord_role(current_user.discord_id, rid)
                for rid in app_type.get_roles_remove_on_submit():
                    remove_discord_role(current_user.discord_id, rid)
            flash('Application submitted! You will be notified when it is reviewed.', 'success')

        # ── Notify staff via webhook (adds 🚨 UNDER 18 flag if applicable) ──────
        trigger_webhooks(application, 'submit', under_18=is_under_18)

        # ── Send DM to user if under 18 ────────────────────────────────────────
        if is_under_18 and new_limit and current_user.discord_id:
            embed = _build_restriction_dm(new_limit, is_new=True)
            send_discord_dm(current_user.discord_id, embeds=[embed])

        AuditLog.log('application.submit', user_id=current_user.id,
                     resource_type='application', resource_id=application.id, ip=request.remote_addr)
        db.session.commit()

        # Redirect back to view; pass under_18_notice so the template can show the banner
        if is_under_18:
            return redirect(url_for('applications.view_application',
                                    app_id=application.id, under_18_notice=1))
        return redirect(url_for('applications.view_application', app_id=application.id))

    return render_template('applications/apply.html', app_type=app_type, schema=schema,
                           under_18_notice=request.args.get('under_18_notice'))


@applications_bp.route('/view/<int:app_id>')
@login_required
def view_application(app_id):
    application = Application.query.get_or_404(app_id)
    if application.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    schema    = application.application_type.get_form_schema()
    responses = application.get_responses()
    comments  = ApplicationComment.query.filter_by(
        application_id=app_id, is_internal=False
    ).order_by(ApplicationComment.created_at).all()
    under_18_notice = request.args.get('under_18_notice')
    return render_template('applications/view.html',
        application=application, schema=schema, responses=responses,
        comments=comments, under_18_notice=under_18_notice)


@applications_bp.route('/view/<int:app_id>/withdraw', methods=['POST'])
@login_required
def withdraw(app_id):
    application = Application.query.get_or_404(app_id)
    if application.user_id != current_user.id:
        abort(403)
    if application.status not in ('pending', 'on_hold'):
        flash('This application cannot be withdrawn.', 'error')
        return redirect(url_for('applications.view_application', app_id=app_id))
    application.status = 'withdrawn'
    db.session.commit()
    flash('Application withdrawn.', 'info')
    return redirect(url_for('applications.my_applications'))


@applications_bp.route('/my')
@login_required
def my_applications():
    apps = Application.query.filter_by(user_id=current_user.id).order_by(Application.submitted_at.desc()).all()
    return render_template('applications/my_applications.html', applications=apps)