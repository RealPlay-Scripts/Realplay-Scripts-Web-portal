
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
