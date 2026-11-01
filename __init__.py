from flask import Flask, render_template, g, request
from flask_login import LoginManager, current_user
from flask_migrate import Migrate
from flask_caching import Cache
from flask_mail import Mail
import os

from app.config import get_config
from app.models import db, User, SiteSettings, Notification

login_manager = LoginManager()
migrate = Migrate()
cache = Cache()
mail = Mail()


def create_app(config_class=None):
    app = Flask(__name__)

    if config_class is None:
        config_class = get_config()
    app.config.from_object(config_class)

    # Ensure upload folder exists
    os.makedirs(app.config.get('UPLOAD_FOLDER', 'app/static/uploads'), exist_ok=True)

    # Init extensions
    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    cache.init_app(app)
    mail.init_app(app)

    # Login manager config
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'warning'

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # Register blueprints
    from app.auth.routes import auth_bp
    from app.admin.routes import admin_bp
    from app.api.routes import api_bp
    from app.api.prems_routes import prems_api_bp
    from app.api.cooldown_routes import cooldown_bp
    from app.applications.routes import applications_bp
    from app.analytics.routes import analytics_bp
    from app.discord_oauth.routes import discord_bp
    from app.profile.routes import profile_bp
    from app.main.routes import main_bp
    from app.reports.routes import reports_bp
    from app.interviews import interviews_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(prems_api_bp, url_prefix='/api/prems')
    app.register_blueprint(cooldown_bp, url_prefix='/api/cooldown')
    app.register_blueprint(applications_bp, url_prefix='/applications')
    app.register_blueprint(analytics_bp, url_prefix='/analytics')
    app.register_blueprint(discord_bp, url_prefix='/auth/discord')
    app.register_blueprint(profile_bp, url_prefix='/profile')
    app.register_blueprint(reports_bp, url_prefix='/reports')
    app.register_blueprint(interviews_bp, url_prefix='/interviews')

    # Context processors
    @app.context_processor
    def inject_globals():
        settings = {}
        try:
            settings = {
                'site_name': SiteSettings.get('site_name', app.config['SITE_NAME']),
                'site_tagline': SiteSettings.get('site_tagline', 'A FiveM Community'),
                'site_logo': SiteSettings.get('site_logo', ''),
                'primary_color': SiteSettings.get('primary_color', '#6366f1'),
                'discord_invite': SiteSettings.get('discord_invite', ''),
            }
        except Exception:
            settings = {
                'site_name': app.config.get('SITE_NAME', 'CFRP Whitelist'),
                'site_tagline': 'A FiveM Community',
                'site_logo': '',
                'primary_color': '#6366f1',
                'discord_invite': '',
            }
        unread_count = 0
        if current_user.is_authenticated:
            try:
                unread_count = Notification.query.filter_by(
                    user_id=current_user.id, is_read=False
                ).count()
            except Exception:
                pass
        return dict(site=settings, unread_notifications=unread_count)

    # Error handlers
    @app.errorhandler(403)
    def forbidden(e):
        return render_template('errors/403.html'), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template('errors/404.html'), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template('errors/500.html'), 500

    # Init DB with defaults
    with app.app_context():
        db.create_all()
        _seed_defaults()
        _clear_legacy_staff_roles(app)

    # Start live economy flag monitor (20s check loop)
    from app.analytics.routes import start_economy_monitor
    start_economy_monitor(app)

    return app


def _clear_legacy_staff_roles(app):
    """
    One-time cleanup: nulls out the legacy discord_role_on_approve / discord_role_on_deny
    fields if they contain the staff role ID.  These fields are invisible in the admin UI
    but are still read as a fallback — this is what caused staff roles being granted on approval.
    """
    import os
    staff_role_id = str(app.config.get('STAFF_ROLE_ID', '') or os.environ.get('STAFF_ROLE_ID', ''))
    if not staff_role_id:
        return
    try:
        from app.models import ApplicationType
        import logging
        log = logging.getLogger("cfrp.init")
        fixed = 0
        for at in ApplicationType.query.all():
            changed = False
            if str(at.discord_role_on_approve or '') == staff_role_id:
                at.discord_role_on_approve = None
                changed = True
            if str(at.discord_role_on_deny or '') == staff_role_id:
                at.discord_role_on_deny = None
                changed = True
            if changed:
                fixed += 1
        if fixed:
            db.session.commit()
            log.warning(
                "Startup cleanup: cleared staff role (%s) from legacy role fields "
                "on %d application type(s). This was causing staff roles to be "
                "granted to users on approval.",
                staff_role_id, fixed,
            )
    except Exception as e:
        import logging
        logging.getLogger("cfrp.init").error("_clear_legacy_staff_roles failed: %s", e)


def _seed_defaults():
    """Seed default roles, permissions, and settings"""
    from app.models import Role, Permission, SiteSettings

    # Create permissions
    for perm_name, description, category in Permission.PERMISSIONS:
        if not Permission.query.filter_by(name=perm_name).first():
            perm = Permission(name=perm_name, description=description, category=category)
            db.session.add(perm)

    # Create system roles
    system_roles = [
        {
            'name': 'admin',
            'display_name': 'Administrator',
            'description': 'Full system access',
            'color': '#ef4444',
            'icon': 'shield-check',
            'priority': 100,
            'is_system': True,
            'perms': [p[0] for p in Permission.PERMISSIONS],
        },
        {
            'name': 'staff',
            'display_name': 'Staff',
            'description': 'Can review applications',
            'color': '#f59e0b',
            'icon': 'user-shield',
            'priority': 50,
            'is_system': True,
            'perms': ['admin.access', 'applications.review', 'applications.approve', 'analytics.view'],
        },
        {
            'name': 'member',
            'display_name': 'Member',
            'description': 'Approved community member',
            'color': '#22c55e',
            'icon': 'user',
            'priority': 10,
            'is_system': True,
            'perms': ['api.access'],
        },
        {
            'name': 'guest',
            'display_name': 'Guest',
            'description': 'Unverified user',
            'color': '#6b7280',
            'icon': 'user-circle',
            'priority': 0,
            'is_system': True,
            'perms': [],
        },
    ]

    for role_data in system_roles:
        role = Role.query.filter_by(name=role_data['name']).first()
        if not role:
            role = Role(
                name=role_data['name'],
                display_name=role_data['display_name'],
                description=role_data['description'],
                color=role_data['color'],
                icon=role_data['icon'],
                priority=role_data['priority'],
                is_system=role_data['is_system'],
            )
            db.session.add(role)
            db.session.flush()

        # Assign permissions
        perms = Permission.query.filter(Permission.name.in_(role_data['perms'])).all()
        role.permissions = perms

    # Site settings defaults
    for key, (value, vtype, desc, cat) in SiteSettings.DEFAULTS.items():
        if not SiteSettings.query.filter_by(key=key).first():
            s = SiteSettings(key=key, value=value, value_type=vtype, description=desc, category=cat)
            db.session.add(s)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
