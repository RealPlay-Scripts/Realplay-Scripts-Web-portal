from datetime import datetime, timezone, timedelta
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
import json

db = SQLAlchemy()


# ─── Association Tables ────────────────────────────────────────────────────────

user_roles = db.Table('user_roles',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('role_id', db.Integer, db.ForeignKey('roles.id'), primary_key=True)
)

role_permissions = db.Table('role_permissions',
    db.Column('role_id', db.Integer, db.ForeignKey('roles.id'), primary_key=True),
    db.Column('permission_id', db.Integer, db.ForeignKey('permissions.id'), primary_key=True)
)

application_type_roles = db.Table('application_type_roles',
    db.Column('application_type_id', db.Integer, db.ForeignKey('application_types.id'), primary_key=True),
    db.Column('role_id', db.Integer, db.ForeignKey('roles.id'), primary_key=True)
)


# ─── Permission Model ──────────────────────────────────────────────────────────

class Permission(db.Model):
    __tablename__ = 'permissions'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    description = db.Column(db.String(256))
    category = db.Column(db.String(64), default='general')

    PERMISSIONS = [
        ('admin.access', 'Access admin panel', 'admin'),
        ('admin.users', 'Manage users', 'admin'),
        ('admin.roles', 'Manage roles', 'admin'),
        ('admin.settings', 'Configure site settings', 'admin'),
        ('admin.branding', 'Configure branding', 'admin'),
        ('applications.create_type', 'Create application types', 'applications'),
        ('applications.review', 'Review applications', 'applications'),
        ('applications.approve', 'Approve/deny applications', 'applications'),
        ('applications.delete', 'Delete applications', 'applications'),
        ('api.access', 'Access API', 'api'),
        ('api.admin', 'Admin API access', 'api'),
        ('analytics.view', 'View analytics', 'analytics'),
        ('discord.sync', 'Sync Discord roles', 'discord'),
        ('reports.manage', 'View and manage reports', 'reports'),
    ]

    def __repr__(self):
        return f'<Permission {self.name}>'


# ─── Role Model ────────────────────────────────────────────────────────────────

class Role(db.Model):
    __tablename__ = 'roles'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    display_name = db.Column(db.String(64), nullable=False)
    description = db.Column(db.String(256))
    color = db.Column(db.String(7), default='#6366f1')  # hex color
    icon = db.Column(db.String(64), default='shield')
    is_system = db.Column(db.Boolean, default=False)  # can't be deleted
    discord_role_id = db.Column(db.String(32))  # linked Discord role
    priority = db.Column(db.Integer, default=0)  # higher = more priority
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    permissions = db.relationship('Permission', secondary=role_permissions, backref='roles')

    def has_permission(self, perm_name):
        return any(p.name == perm_name for p in self.permissions)

    def __repr__(self):
        return f'<Role {self.name}>'


# ─── User Model ────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)

    # Discord
    discord_id = db.Column(db.String(32), unique=True, index=True)
    discord_username = db.Column(db.String(64))
    discord_avatar = db.Column(db.String(128))
    discord_access_token = db.Column(db.String(256))
    discord_refresh_token = db.Column(db.String(256))
    discord_token_expires = db.Column(db.DateTime(timezone=True))
    discord_verified = db.Column(db.Boolean, default=False)

    # 2FA
    totp_secret = db.Column(db.String(32))
    totp_enabled = db.Column(db.Boolean, default=False)
    backup_codes = db.Column(db.Text)  # JSON list

    # Status
    is_active = db.Column(db.Boolean, default=True)
    is_banned = db.Column(db.Boolean, default=False)
    ban_reason = db.Column(db.String(512))
    email_verified = db.Column(db.Boolean, default=False)
    email_verify_token = db.Column(db.String(128))

    # Profile
    bio = db.Column(db.Text)
    avatar = db.Column(db.String(256))  # local upload path
    timezone = db.Column(db.String(64), default='UTC')

    # Stats
    last_login = db.Column(db.DateTime(timezone=True))
    last_ip = db.Column(db.String(45))
    login_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    roles = db.relationship('Role', secondary=user_roles, backref='users')
    applications = db.relationship('Application', backref='applicant', lazy='dynamic', foreign_keys='Application.user_id')
    api_keys = db.relationship('APIKey', backref='user', lazy='dynamic')
    notifications = db.relationship('Notification', backref='user', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def has_permission(self, perm_name):
        for role in self.roles:
            if role.has_permission(perm_name):
                return True
        return False

    def has_role(self, role_name):
        return any(r.name == role_name for r in self.roles)

    @property
    def is_admin(self):
        return self.has_permission('admin.access') or self.has_role('admin')

    @property
    def primary_role(self):
        if not self.roles:
            return None
        return max(self.roles, key=lambda r: r.priority)

    @property
    def discord_avatar_url(self):
        if self.discord_id and self.discord_avatar:
            return f"https://cdn.discordapp.com/avatars/{self.discord_id}/{self.discord_avatar}.png"
        return None

    def get_backup_codes(self):
        if self.backup_codes:
            return json.loads(self.backup_codes)
        return []

    def set_backup_codes(self, codes):
        self.backup_codes = json.dumps(codes)

    def generate_backup_codes(self):
        codes = [secrets.token_hex(4).upper() + '-' + secrets.token_hex(4).upper() for _ in range(8)]
        self.set_backup_codes(codes)
        return codes

    def to_dict(self, include_private=False):
        data = {
            'id': self.id,
            'username': self.username,
            'discord_id': self.discord_id,
            'discord_username': self.discord_username,
            'discord_avatar': self.discord_avatar_url,
            'discord_verified': self.discord_verified,
            'roles': [{'id': r.id, 'name': r.name, 'display_name': r.display_name, 'color': r.color} for r in self.roles],
            'primary_role': {'name': self.primary_role.name, 'display_name': self.primary_role.display_name, 'color': self.primary_role.color} if self.primary_role else None,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None,
        }
        if include_private:
            data['email'] = self.email
            data['totp_enabled'] = self.totp_enabled
            data['email_verified'] = self.email_verified
        return data

    def __repr__(self):
        return f'<User {self.username}>'


# ─── API Key Model ─────────────────────────────────────────────────────────────

class APIKey(db.Model):
    __tablename__ = 'api_keys'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    prefix = db.Column(db.String(8))  # first 8 chars for display
    scopes = db.Column(db.Text, server_default='[]')  # JSON list of scopes
    is_active = db.Column(db.Boolean, default=True)
    last_used = db.Column(db.DateTime(timezone=True))
    use_count = db.Column(db.Integer, default=0)
    expires_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    @classmethod
    def generate(cls, user_id, name, scopes=None):
        raw_key = 'cfrp_' + secrets.token_urlsafe(48)
        key = cls(
            user_id=user_id,
            name=name,
            key=raw_key,
            prefix=raw_key[:12] + '...',
            scopes=json.dumps(scopes or ['read']),
        )
        return key, raw_key

    def get_scopes(self):
        return json.loads(self.scopes) if self.scopes else []

    def has_scope(self, scope):
        scopes = self.get_scopes()
        return scope in scopes or 'admin' in scopes

    def __repr__(self):
        return f'<APIKey {self.prefix}>'


# ─── Application Type Model ────────────────────────────────────────────────────

class ApplicationType(db.Model):
    __tablename__ = 'application_types'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    slug = db.Column(db.String(128), unique=True, nullable=False, index=True)
    description = db.Column(db.Text)
    icon = db.Column(db.String(64), default='document-text')
    color = db.Column(db.String(7), default='#6366f1')
    is_active = db.Column(db.Boolean, default=True)
    requires_discord = db.Column(db.Boolean, default=True)
    max_applications = db.Column(db.Integer, default=1)  # per user, 0=unlimited
    cooldown_days = db.Column(db.Integer, default=30)  # days before reapply
    auto_approve = db.Column(db.Boolean, default=False)
    discord_roles_on_approve = db.Column(db.Text, server_default='[]')   # JSON list of role IDs to add
    discord_roles_on_deny    = db.Column(db.Text, server_default='[]')   # JSON list of role IDs to add
    discord_roles_on_submit  = db.Column(db.Text, server_default='[]')   # JSON list of role IDs to add on submit
    discord_roles_remove_on_submit  = db.Column(db.Text, server_default='[]')  # role IDs to remove on submit
    discord_roles_remove_on_approve = db.Column(db.Text, server_default='[]')  # role IDs to remove on approve
    discord_roles_remove_on_deny    = db.Column(db.Text, server_default='[]')  # role IDs to remove on deny

    # Legacy single-role fields kept for backwards compat (ignored if JSON fields set)
    discord_role_on_approve = db.Column(db.String(32))
    discord_role_on_deny    = db.Column(db.String(32))

    def get_roles_on_approve(self):
        # NOTE: The legacy `discord_role_on_approve` single-role field is intentionally
        # NOT used as a fallback here. It can contain stale role IDs (e.g. the staff role)
        # that are invisible in the admin UI, causing unexpected role assignments.
        # All role management must go through the JSON fields shown in the UI.
        try: return json.loads(self.discord_roles_on_approve or '[]') or []
        except: return []

    def get_roles_on_deny(self):
        try: return json.loads(self.discord_roles_on_deny or '[]') or []
        except: return []

    def get_roles_on_submit(self):
        try: return json.loads(self.discord_roles_on_submit or '[]')
        except: return []

    def get_roles_remove_on_submit(self):
        try: return json.loads(self.discord_roles_remove_on_submit or '[]')
        except: return []

    def get_roles_remove_on_approve(self):
        try: return json.loads(self.discord_roles_remove_on_approve or '[]')
        except: return []

    def get_roles_remove_on_deny(self):
        try: return json.loads(self.discord_roles_remove_on_deny or '[]')
        except: return []

    sort_order = db.Column(db.Integer, default=0)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Visibility roles (empty = visible to all staff with review perms)
    visible_to_roles = db.relationship('Role', secondary=application_type_roles, backref='application_types')

    # Form fields (JSON)
    form_schema = db.Column(db.Text, server_default='[]')  # JSON list of field definitions
    webhook_url = db.Column(db.String(512))
    webhook_config = db.Column(db.Text, default='{}')  # JSON webhook settings

    applications = db.relationship('Application', backref='application_type', lazy='dynamic', cascade='all, delete-orphan')
    webhooks = db.relationship('Webhook', backref='application_type', lazy='dynamic')

    def get_form_schema(self):
        return json.loads(self.form_schema) if self.form_schema else []

    def set_form_schema(self, schema):
        self.form_schema = json.dumps(schema)

    def get_webhook_config(self):
        return json.loads(self.webhook_config) if self.webhook_config else {}

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'slug': self.slug,
            'description': self.description,
            'is_active': self.is_active,
            'requires_discord': self.requires_discord,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<ApplicationType {self.name}>'


# ─── Application Model ─────────────────────────────────────────────────────────

class Application(db.Model):
    __tablename__ = 'applications'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    type_id = db.Column(db.Integer, db.ForeignKey('application_types.id'), nullable=False)

    STATUS_PENDING = 'pending'
    STATUS_UNDER_REVIEW = 'under_review'
    STATUS_APPROVED = 'approved'
    STATUS_DENIED = 'denied'
    STATUS_WITHDRAWN = 'withdrawn'
    STATUS_ON_HOLD = 'on_hold'

    status = db.Column(db.String(32), default='pending', index=True)
    responses = db.Column(db.Text, default='{}')  # JSON field responses
    notes = db.Column(db.Text)  # internal reviewer notes

    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    reviewed_at = db.Column(db.DateTime(timezone=True))
    review_note = db.Column(db.Text)  # message shown to applicant

    webhook_sent = db.Column(db.Boolean, default=False)
    webhook_sent_at = db.Column(db.DateTime(timezone=True))

    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(512))

    submitted_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    reviewer = db.relationship('User', foreign_keys=[reviewed_by], backref='reviewed_applications')
    comments = db.relationship('ApplicationComment', backref='application', lazy='dynamic', cascade='all, delete-orphan')

    def get_responses(self):
        return json.loads(self.responses) if self.responses else {}

    def set_responses(self, data):
        self.responses = json.dumps(data)

    @property
    def status_badge_class(self):
        classes = {
            'pending': 'yellow',
            'under_review': 'blue',
            'approved': 'green',
            'denied': 'red',
            'withdrawn': 'gray',
            'on_hold': 'orange',
        }
        return classes.get(self.status, 'gray')

    def to_dict(self):
        return {
            'id': self.id,
            'user': self.applicant.to_dict(),
            'type': self.application_type.to_dict(),
            'status': self.status,
            'responses': self.get_responses(),
            'review_note': self.review_note,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'submitted_at': self.submitted_at.isoformat() if self.submitted_at else None,
        }

    def __repr__(self):
        return f'<Application {self.id} [{self.status}]>'


class ApplicationComment(db.Model):
    __tablename__ = 'application_comments'
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('applications.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    is_internal = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    author = db.relationship('User', backref='application_comments')


# ─── Webhook Model ────────────────────────────────────────────────────────────

class Webhook(db.Model):
    __tablename__ = 'webhooks'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    type_id = db.Column(db.Integer, db.ForeignKey('application_types.id'))
    url = db.Column(db.String(512), nullable=False)
    secret = db.Column(db.String(128))
    is_active = db.Column(db.Boolean, default=True)

    # Trigger events
    on_submit = db.Column(db.Boolean, default=True)
    on_approve = db.Column(db.Boolean, default=True)
    on_deny = db.Column(db.Boolean, default=True)
    on_review = db.Column(db.Boolean, default=False)
    on_hold = db.Column(db.Boolean, default=False)

    # Discord embed customization (JSON)
    embed_config = db.Column(db.Text, default='{}')

    last_triggered = db.Column(db.DateTime(timezone=True))
    trigger_count = db.Column(db.Integer, default=0)
    last_status_code = db.Column(db.Integer)
    last_error = db.Column(db.Text)

    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    creator = db.relationship('User', foreign_keys=[created_by])
    delivery_logs = db.relationship('WebhookDelivery', backref='webhook', lazy='dynamic', cascade='all, delete-orphan')

    def get_embed_config(self):
        return json.loads(self.embed_config) if self.embed_config else {}

    def __repr__(self):
        return f'<Webhook {self.name}>'


class WebhookDelivery(db.Model):
    __tablename__ = 'webhook_deliveries'
    id = db.Column(db.Integer, primary_key=True)
    webhook_id = db.Column(db.Integer, db.ForeignKey('webhooks.id'), nullable=False)
    application_id = db.Column(db.Integer, db.ForeignKey('applications.id'))
    event = db.Column(db.String(32))
    payload = db.Column(db.Text)
    status_code = db.Column(db.Integer)
    response_body = db.Column(db.Text)
    success = db.Column(db.Boolean, default=False)
    duration_ms = db.Column(db.Integer)
    attempted_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ─── FiveM Player Session ──────────────────────────────────────────────────────

class PlayerSession(db.Model):
    __tablename__ = 'player_sessions'
    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(32), nullable=False, index=True)
    player_name = db.Column(db.String(128))
    join_time = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    leave_time = db.Column(db.DateTime(timezone=True))
    duration_seconds = db.Column(db.Integer)
    disconnect_reason = db.Column(db.String(512))
    disconnect_category = db.Column(db.String(32))  # quit/crash/timeout/kick/ban/etc.
    last_x = db.Column(db.Float)
    last_y = db.Column(db.Float)
    last_z = db.Column(db.Float)
    disconnect_ping = db.Column(db.Integer)

    # Economy snapshot at session end
    cash_at_leave = db.Column(db.Integer)
    bank_at_leave = db.Column(db.Integer)

    @property
    def is_online(self):
        return self.leave_time is None

    @property
    def duration_minutes(self):
        if self.duration_seconds:
            return self.duration_seconds // 60
        if self.is_online:
            join_aware = self.join_time if self.join_time.tzinfo else self.join_time.replace(tzinfo=timezone.utc)
            diff = datetime.now(timezone.utc) - join_aware
            return int(diff.total_seconds()) // 60
        return 0

    def __repr__(self):
        return f'<PlayerSession {self.discord_id} [{self.join_time}]>'


class PlayerHeartbeat(db.Model):
    """Tracks currently online players via heartbeat"""
    __tablename__ = 'player_heartbeats'
    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(32), unique=True, nullable=False, index=True)
    player_name = db.Column(db.String(128))
    last_seen = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    session_id = db.Column(db.Integer, db.ForeignKey('player_sessions.id'))
    x = db.Column(db.Float)
    y = db.Column(db.Float)
    z = db.Column(db.Float)
    cash = db.Column(db.Integer, default=0)
    bank = db.Column(db.Integer, default=0)
    ping = db.Column(db.Integer, default=0)


# ─── FiveM Kill/Death Stats ────────────────────────────────────────────────────

class PlayerStat(db.Model):
    __tablename__ = 'player_stats'
    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(32), nullable=False, index=True)
    citizenid = db.Column(db.String(64), index=True)
    event_type = db.Column(db.String(16), nullable=False)  # 'kill' or 'death'
    cause = db.Column(db.String(64))
    weapon = db.Column(db.String(64))
    killer_discord_id = db.Column(db.String(32), index=True)
    killer_citizenid = db.Column(db.String(64))
    killer_name = db.Column(db.String(128))
    victim_name = db.Column(db.String(128))
    x = db.Column(db.Float)
    y = db.Column(db.Float)
    z = db.Column(db.Float)
    recorded_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    def __repr__(self):
        return f'<PlayerStat {self.discord_id} {self.event_type}>'


# ─── Playtime Aggregate ────────────────────────────────────────────────────────

class PlaytimeAggregate(db.Model):
    """Pre-computed daily playtime per player for leaderboard performance"""
    __tablename__ = 'playtime_aggregates'
    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(32), nullable=False, index=True)
    player_name = db.Column(db.String(128))
    date = db.Column(db.Date, nullable=False, index=True)
    seconds = db.Column(db.Integer, default=0)

    __table_args__ = (db.UniqueConstraint('discord_id', 'date'),)


# ─── Site Settings ─────────────────────────────────────────────────────────────

class SiteSettings(db.Model):
    __tablename__ = 'site_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(128), unique=True, nullable=False, index=True)
    value = db.Column(db.Text)
    value_type = db.Column(db.String(16), default='string')  # string/bool/int/json
    description = db.Column(db.String(256))
    category = db.Column(db.String(64), default='general')
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    @classmethod
    def get(cls, key, default=None):
        setting = cls.query.filter_by(key=key).first()
        if not setting:
            return default
        if setting.value_type == 'bool':
            return setting.value.lower() == 'true'
        if setting.value_type == 'int':
            return int(setting.value) if setting.value else default
        if setting.value_type == 'json':
            return json.loads(setting.value) if setting.value else default
        return setting.value

    @classmethod
    def set(cls, key, value, value_type='string', description=None, category='general'):
        setting = cls.query.filter_by(key=key).first()
        if not setting:
            setting = cls(key=key, value_type=value_type, description=description, category=category)
            db.session.add(setting)
        if value_type == 'json':
            setting.value = json.dumps(value)
        elif value_type == 'bool':
            setting.value = 'true' if value else 'false'
        else:
            setting.value = str(value)
        db.session.commit()

    DEFAULTS = {
        'site_name': ('CFRP Whitelist', 'string', 'Site display name', 'branding'),
        'site_tagline': ('A FiveM Community', 'string', 'Site tagline', 'branding'),
        'site_logo': ('', 'string', 'Logo file path', 'branding'),
        'site_favicon': ('', 'string', 'Favicon file path', 'branding'),
        'primary_color': ('#6366f1', 'string', 'Primary accent color', 'branding'),
        'discord_invite': ('', 'string', 'Discord server invite link', 'discord'),
        'require_discord': ('true', 'bool', 'Require Discord to apply', 'applications'),
        'maintenance_mode': ('false', 'bool', 'Enable maintenance mode', 'general'),
        'allow_registration': ('true', 'bool', 'Allow new registrations', 'general'),
        'fivem_server_ip': ('', 'string', 'FiveM server IP:port for connect link', 'fivem'),
        # ── Whitelist ──────────────────────────────────────────────────────────
        'whitelist_enabled': ('true', 'bool', 'FiveM whitelist active', 'whitelist'),
        'whitelist_kick_msg': ('You are not whitelisted on this server!\n\nTo join, please visit our Discord: https://discord.gg/VpWAtzPw9Z and apply.\n\nYou must link your Discord account on our website:\n%s\n\nOnce linked, restart FiveM and try again.', 'string', 'Kick message when whitelist is off', 'whitelist'),
        'whitelist_closed_msg': ('The server whitelist is currently closed. Check our Discord for updates.', 'string', 'Message shown when whitelist is disabled', 'whitelist'),
    }

    def __repr__(self):
        return f'<Setting {self.key}={self.value}>'


# ─── Notification Model ────────────────────────────────────────────────────────

class Notification(db.Model):
    __tablename__ = 'notifications'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(128), nullable=False)
    message = db.Column(db.Text, nullable=False)
    type = db.Column(db.String(32), default='info')  # info/success/warning/error
    link = db.Column(db.String(256))
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'message': self.message,
            'type': self.type,
            'link': self.link,
            'is_read': self.is_read,
            'created_at': self.created_at.isoformat(),
        }


# ─── Audit Log ────────────────────────────────────────────────────────────────

class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    action = db.Column(db.String(128), nullable=False)
    resource_type = db.Column(db.String(64))
    resource_id = db.Column(db.String(64))
    details = db.Column(db.Text)  # JSON
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    actor = db.relationship('User', foreign_keys=[user_id])

    @classmethod
    def log(cls, action, user_id=None, resource_type=None, resource_id=None, details=None, ip=None):
        entry = cls(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else None,
            details=json.dumps(details) if details else None,
            ip_address=ip,
        )
        db.session.add(entry)
        return entry


# ─── Economy Flag Model ────────────────────────────────────────────────────────

class EconomyFlag(db.Model):
    """Flagged player for suspicious in-game money gains."""
    __tablename__ = 'economy_flags'

    id           = db.Column(db.Integer, primary_key=True)
    discord_id   = db.Column(db.String(32), nullable=False, index=True)
    player_name  = db.Column(db.String(128))
    flag_type    = db.Column(db.String(64), default='large_gain')
    amount_gain  = db.Column(db.Integer)          # net gain that triggered flag ($)
    cash_before  = db.Column(db.Integer)
    bank_before  = db.Column(db.Integer)
    cash_after   = db.Column(db.Integer)
    bank_after   = db.Column(db.Integer)
    window_start = db.Column(db.DateTime(timezone=True))         # 24h window start
    window_end   = db.Column(db.DateTime(timezone=True))
    detected_at  = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    is_reviewed  = db.Column(db.Boolean, default=False, index=True)
    reviewed_by  = db.Column(db.Integer, db.ForeignKey('users.id'))
    review_note  = db.Column(db.Text)
    reviewed_at  = db.Column(db.DateTime(timezone=True))
    discord_message_id = db.Column(db.String(32))  # for editing the webhook message
    dedup_key = db.Column(db.String(64), unique=True, nullable=True, index=True)

    reviewer = db.relationship('User', foreign_keys=[reviewed_by])

    def to_dict(self):
        return {
            'id':           self.id,
            'discord_id':   self.discord_id,
            'player_name':  self.player_name or 'Unknown',
            'flag_type':    self.flag_type,
            'amount_gain':  self.amount_gain,
            'cash_before':  self.cash_before,
            'bank_before':  self.bank_before,
            'cash_after':   self.cash_after,
            'bank_after':   self.bank_after,
            'window_start': self.window_start.isoformat() if self.window_start else None,
            'window_end':   self.window_end.isoformat()   if self.window_end   else None,
            'detected_at':  self.detected_at.isoformat()  if self.detected_at  else None,
            'is_reviewed':  self.is_reviewed,
            'review_note':  self.review_note,
        }



class InterviewSlot(db.Model):
    """A time slot that can be blocked by staff to prevent bookings."""
    __tablename__ = 'interview_slots'

    id          = db.Column(db.Integer, primary_key=True)
    slot_date   = db.Column(db.Date, nullable=True, index=True)
    slot_time   = db.Column(db.Time, nullable=False)
    is_blocked  = db.Column(db.Boolean, default=False)
    blocked_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    block_note  = db.Column(db.String(256))
    created_at  = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    blocker = db.relationship('User', foreign_keys=[blocked_by])


class Interview(db.Model):
    """A scheduled interview between a user and staff."""
    __tablename__ = 'interviews'

    STATUSES = ['pending', 'confirmed', 'completed', 'cancelled', 'no_show']
    STATUS_LABELS = {
        'pending':   'Pending Confirmation',
        'confirmed': 'Confirmed',
        'completed': 'Completed',
        'cancelled': 'Cancelled',
        'no_show':   'No Show',
    }
    STATUS_COLORS = {
        'pending':   '#f59e0b',
        'confirmed': '#6366f1',
        'completed': '#22c55e',
        'cancelled': '#6b7280',
        'no_show':   '#ef4444',
    }

    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    interviewer_id   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    interview_date   = db.Column(db.Date, nullable=False)
    interview_time   = db.Column(db.Time, nullable=False)
    status           = db.Column(db.String(32), default='pending', index=True)
    notes            = db.Column(db.Text)
    staff_notes      = db.Column(db.Text)
    cancelled_by     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    cancel_reason    = db.Column(db.String(512))
    created_at       = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at       = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                                 onupdate=lambda: datetime.now(timezone.utc))

    applicant   = db.relationship('User', foreign_keys=[user_id],       backref='interviews')
    interviewer = db.relationship('User', foreign_keys=[interviewer_id])
    canceller   = db.relationship('User', foreign_keys=[cancelled_by])

    @property
    def status_label(self):
        return self.STATUS_LABELS.get(self.status, self.status.title())

    @property
    def status_color(self):
        return self.STATUS_COLORS.get(self.status, '#6b7280')

    @property
    def datetime_sast(self):
        return f"{self.interview_date.strftime('%d %b %Y')} at {self.interview_time.strftime('%H:%M')} SAST"


class FlagLimit(db.Model):
    """Per-player or global flag threshold overrides.
    
    If discord_id is NULL → this is the global default row (there should be only one).
    If discord_id is set  → this overrides the threshold for that specific player.
    """
    __tablename__ = 'flag_limits'

    id          = db.Column(db.Integer, primary_key=True)
    discord_id  = db.Column(db.String(32), unique=True, nullable=True, index=True)  # NULL = global
    player_name = db.Column(db.String(128))   # cached display name
    threshold   = db.Column(db.Integer, nullable=False)   # $ gain that triggers a flag
    note        = db.Column(db.String(256))   # optional staff note
    updated_by  = db.Column(db.Integer, db.ForeignKey('users.id'))
    updated_at  = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                            onupdate=lambda: datetime.now(timezone.utc))

    updater = db.relationship('User', foreign_keys=[updated_by])

    @staticmethod
    def get_threshold(discord_id: str) -> int:
        """Return the effective threshold for a player — per-player override or global default."""
        per_player = FlagLimit.query.filter_by(discord_id=discord_id).first()
        if per_player:
            return per_player.threshold
        global_row = FlagLimit.query.filter_by(discord_id=None).first()
        if global_row:
            return global_row.threshold
        from app.analytics.routes import FLAG_THRESHOLD
        return FLAG_THRESHOLD

    def to_dict(self):
        return {
            'id':          self.id,
            'discord_id':  self.discord_id,
            'player_name': self.player_name or '—',
            'threshold':   self.threshold,
            'note':        self.note or '',
            'updated_at':  self.updated_at.isoformat() if self.updated_at else None,
            'updater':     self.updater.username if self.updater else '—',
        }


# ─── Whitelist Schedule Model ─────────────────────────────────────────────────

class WhitelistSchedule(db.Model):
    """
    Scheduled whitelist on/off events.
    Each row represents one future (or recurring) event:
      - enabled=True  → turn whitelist ON at scheduled_at
      - enabled=False → turn whitelist OFF at scheduled_at
    repeat_type:
      'once'    — fire once and mark is_executed=True
      'daily'   — repeat every day at the stored HH:MM
      'weekly'  — repeat every week on stored weekday (0=Mon…6=Sun)
    """
    __tablename__ = 'whitelist_schedules'

    id           = db.Column(db.Integer, primary_key=True)
    label        = db.Column(db.String(128), nullable=False, default='')
    enabled      = db.Column(db.Boolean, nullable=False)          # True = turn ON, False = turn OFF
    scheduled_at = db.Column(db.DateTime(timezone=True), nullable=False)         # UTC datetime for next fire
    repeat_type  = db.Column(db.String(16), default='once')       # once / daily / weekly
    is_executed  = db.Column(db.Boolean, default=False)           # for 'once' schedules
    created_by   = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at   = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'id':           self.id,
            'label':        self.label,
            'enabled':      self.enabled,
            'scheduled_at': self.scheduled_at.isoformat() + 'Z',
            'repeat_type':  self.repeat_type,
            'is_executed':  self.is_executed,
            'created_at':   self.created_at.isoformat() + 'Z',
            'created_by':   self.created_by,
        }

    def __repr__(self):
        state = 'ON' if self.enabled else 'OFF'
        return f'<WhitelistSchedule {state} @ {self.scheduled_at} ({self.repeat_type})>'

# ─── Report System ────────────────────────────────────────────────────────────

class Report(db.Model):
    __tablename__ = 'reports'

    STATUSES    = ['pending', 'seen', 'in_progress', 'waiting', 'resolved']
    STATUS_LABELS = {
        'pending':     'Pending',
        'seen':        'Seen',
        'in_progress': 'In Progress',
        'waiting':     'Waiting',
        'resolved':    'Resolved',
    }
    STATUS_COLORS = {
        'pending':     '#f59e0b',
        'seen':        '#6366f1',
        'in_progress': '#3b82f6',
        'waiting':     '#f97316',
        'resolved':    '#22c55e',
    }
    STATUS_ICONS = {
        'pending':     'clock',
        'seen':        'eye',
        'in_progress': 'loader',
        'waiting':     'hourglass',
        'resolved':    'circle-check',
    }

    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    report_type      = db.Column(db.String(16), nullable=False)   # 'player' or 'bug'
    title            = db.Column(db.String(256), nullable=False)
    description      = db.Column(db.Text, nullable=False)

    # Player-report extras
    reported_player  = db.Column(db.String(128))   # name/ID of reported player
    reported_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # linked website user
    server_id        = db.Column(db.String(64))    # optional server / session ID

    # Bug-report extras
    steps_to_reproduce = db.Column(db.Text)
    expected_behavior  = db.Column(db.Text)

    status           = db.Column(db.String(32), default='pending', index=True)
    assigned_to      = db.Column(db.Integer, db.ForeignKey('users.id'))
    staff_note       = db.Column(db.Text)        # internal staff-only note

    discord_message_id = db.Column(db.String(32))  # track the webhook message

    created_at       = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    updated_at       = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                                 onupdate=lambda: datetime.now(timezone.utc))
    resolved_at      = db.Column(db.DateTime(timezone=True))
    resolved_by      = db.Column(db.Integer, db.ForeignKey('users.id'))

    # Relationships
    reporter      = db.relationship('User', foreign_keys=[user_id],        backref='reports')
    assignee      = db.relationship('User', foreign_keys=[assigned_to])
    resolver      = db.relationship('User', foreign_keys=[resolved_by])
    reported_user = db.relationship('User', foreign_keys=[reported_user_id])
    messages      = db.relationship('ReportMessage', backref='report',
                                    lazy='dynamic', cascade='all, delete-orphan',
                                    order_by='ReportMessage.created_at')
    suspect_messages = db.relationship('ReportSuspectMessage', backref='report',
                                       lazy='dynamic', cascade='all, delete-orphan',
                                       order_by='ReportSuspectMessage.created_at')

    @property
    def status_label(self):
        return self.STATUS_LABELS.get(self.status, self.status.title())

    @property
    def status_color(self):
        return self.STATUS_COLORS.get(self.status, '#6b7280')

    @property
    def status_icon(self):
        return self.STATUS_ICONS.get(self.status, 'circle')

    @property
    def progress_pct(self):
        steps = self.STATUSES
        idx   = steps.index(self.status) if self.status in steps else 0
        return int((idx / (len(steps) - 1)) * 100)

    @property
    def unread_staff_messages(self):
        """Messages from staff that the reporter hasn't seen."""
        return self.messages.filter_by(is_staff=True, is_read=False).count()

    def to_dict(self):
        return {
            'id':          self.id,
            'type':        self.report_type,
            'title':       self.title,
            'status':      self.status,
            'created_at':  self.created_at.isoformat() if self.created_at else None,
            'reporter':    self.reporter.username if self.reporter else None,
        }

    def __repr__(self):
        return f'<Report {self.id} [{self.report_type}] {self.status}>'


class ReportMessage(db.Model):
    __tablename__ = 'report_messages'

    id         = db.Column(db.Integer, primary_key=True)
    report_id  = db.Column(db.Integer, db.ForeignKey('reports.id'), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'),   nullable=False)
    content    = db.Column(db.Text, nullable=False)
    is_staff   = db.Column(db.Boolean, default=False)
    is_read    = db.Column(db.Boolean, default=False)   # True once reporter views it
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    author = db.relationship('User', foreign_keys=[user_id])

    def __repr__(self):
        return f'<ReportMessage {self.id} report={self.report_id}>'


class ReportSuspectMessage(db.Model):
    """Staff ↔ reported-user private communication thread."""
    __tablename__ = 'report_suspect_messages'

    id         = db.Column(db.Integer, primary_key=True)
    report_id  = db.Column(db.Integer, db.ForeignKey('reports.id'), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'),   nullable=False)
    content    = db.Column(db.Text, nullable=False)
    is_staff   = db.Column(db.Boolean, default=False)
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    author = db.relationship('User', foreign_keys=[user_id])

    def __repr__(self):
        return f'<ReportSuspectMessage {self.id} report={self.report_id}>'


# ─── Crash / Timeout Auto-Diagnosis ───────────────────────────────────────────

class CrashDiagnosis(db.Model):
    """
    Auto-generated diagnosis record for every crash/timeout session.
    Created immediately when a session ends with category crash/timeout.
    Linked to a PlayerSession and optionally to a User (via discord_id).
    """
    __tablename__ = 'crash_diagnoses'

    id              = db.Column(db.Integer, primary_key=True)
    session_id      = db.Column(db.Integer, db.ForeignKey('player_sessions.id'), nullable=False, index=True)
    discord_id      = db.Column(db.String(32), nullable=False, index=True)
    player_name     = db.Column(db.String(128))
    category        = db.Column(db.String(32))          # crash / timeout
    reason_raw      = db.Column(db.String(512))         # raw disconnect_reason
    diagnosis_key   = db.Column(db.String(64))          # e.g. connection_timeout
    diagnosis_label = db.Column(db.String(128))         # human label
    diagnosis_text  = db.Column(db.Text)                # explanation paragraph
    fix_steps       = db.Column(db.Text)                # JSON list of strings
    ping_at_crash   = db.Column(db.Integer)
    session_seconds = db.Column(db.Integer)
    detected_at     = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    is_viewed_user  = db.Column(db.Boolean, default=False)  # user has seen it on profile
    is_viewed_admin = db.Column(db.Boolean, default=False)  # admin has dismissed it

    session = db.relationship('PlayerSession', backref=db.backref('diagnosis', uselist=False))

    def fix_steps_list(self):
        try:
            import json as _j
            return _j.loads(self.fix_steps) if self.fix_steps else []
        except Exception:
            return []

    def to_dict(self):
        return {
            'id':               self.id,
            'session_id':       self.session_id,
            'discord_id':       self.discord_id,
            'player_name':      self.player_name,
            'category':         self.category,
            'reason_raw':       self.reason_raw,
            'diagnosis_key':    self.diagnosis_key,
            'diagnosis_label':  self.diagnosis_label,
            'diagnosis_text':   self.diagnosis_text,
            'fix_steps':        self.fix_steps_list(),
            'ping_at_crash':    self.ping_at_crash,
            'session_seconds':  self.session_seconds,
            'detected_at':      self.detected_at.isoformat() if self.detected_at else None,
            'is_viewed_user':   self.is_viewed_user,
            'is_viewed_admin':  self.is_viewed_admin,
        }

    def __repr__(self):
        return f'<CrashDiagnosis {self.id} [{self.category}] {self.player_name}>'


# ─── Update Log ────────────────────────────────────────────────────────────────

class UpdateLog(db.Model):
    """Admin-authored server update log entries, posted to Discord."""
    __tablename__ = 'update_logs'
    id            = db.Column(db.Integer, primary_key=True)
    version       = db.Column(db.String(32))                     # e.g. "1.4.2" or "hotfix-03"
    title         = db.Column(db.String(256), nullable=False)
    summary       = db.Column(db.Text)                           # short one-liner shown in embed footer
    category      = db.Column(db.String(32), default='update')   # update / hotfix / announcement / maintenance
    # Change items stored as JSON list of {type: added|changed|fixed|removed, text: str}
    changes       = db.Column(db.Text, default='[]')
    webhook_url   = db.Column(db.String(512))                    # saved per-entry (inherits page setting)
    discord_sent  = db.Column(db.Boolean, default=False)
    discord_sent_at = db.Column(db.DateTime(timezone=True))
    discord_message_id = db.Column(db.String(32))
    created_by    = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at    = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    updated_at    = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                              onupdate=lambda: datetime.now(timezone.utc))

    author        = db.relationship('User', foreign_keys=[created_by])

    def get_changes(self):
        import json as _j
        try:
            return _j.loads(self.changes) if self.changes else []
        except Exception:
            return []

    def __repr__(self):
        return f'<UpdateLog {self.id} {self.title}>'
# ─── In-Game Admin Permissions ────────────────────────────────────────────────

class InGamePrem(db.Model):
    """
    Tracks in-game admin permissions granted to players.
    Linked to users by discord_id (same key used throughout the platform).
    """
    __tablename__ = 'ingame_prems'

    PERM_LEVELS = ['moderator', 'admin', 'superadmin', 'owner']

    id           = db.Column(db.Integer, primary_key=True)
    discord_id   = db.Column(db.String(32), nullable=False, index=True)
    player_name  = db.Column(db.String(128))                    # cached display name
    license_id   = db.Column(db.String(128))                    # fivem: / steam: / license: identifier
    perm_level   = db.Column(db.String(32), nullable=False, default='moderator')  # see PERM_LEVELS
    custom_perms = db.Column(db.Text, default='[]')             # JSON list of extra ace perms
    is_active    = db.Column(db.Boolean, default=True, index=True)
    note         = db.Column(db.String(512))                    # internal staff note
    granted_by   = db.Column(db.Integer, db.ForeignKey('users.id'))
    revoked_by   = db.Column(db.Integer, db.ForeignKey('users.id'))
    revoked_at   = db.Column(db.DateTime(timezone=True))
    granted_at   = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    expires_at   = db.Column(db.DateTime(timezone=True))        # None = permanent

    grantor = db.relationship('User', foreign_keys=[granted_by])
    revoker = db.relationship('User', foreign_keys=[revoked_by])

    def get_custom_perms(self):
        try:
            return json.loads(self.custom_perms) if self.custom_perms else []
        except Exception:
            return []

    def set_custom_perms(self, perms_list):
        self.custom_perms = json.dumps(perms_list)

    @property
    def is_expired(self):
        if not self.expires_at:
            return False
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def is_currently_active(self):
        return self.is_active and not self.is_expired

    def to_dict(self):
        return {
            'id':           self.id,
            'discord_id':   self.discord_id,
            'player_name':  self.player_name,
            'license_id':   self.license_id,
            'perm_level':   self.perm_level,
            'custom_perms': self.get_custom_perms(),
            'is_active':    self.is_active,
            'is_expired':   self.is_expired,
            'note':         self.note,
            'granted_at':   self.granted_at.isoformat() if self.granted_at else None,
            'expires_at':   self.expires_at.isoformat() if self.expires_at else None,
            'granted_by':   self.grantor.username if self.grantor else None,
        }

    def __repr__(self):
        return f'<InGamePrem {self.discord_id} [{self.perm_level}] active={self.is_active}>'
