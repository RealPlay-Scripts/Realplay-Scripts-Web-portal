"""
WHITELIST INTEGRATION — models.py additions
===========================================
Add these two things to your existing models.py:

1. Add to SiteSettings.DEFAULTS (inside the DEFAULTS dict):
        'whitelist_enabled': ('true', 'bool', 'FiveM whitelist active', 'whitelist'),
        'whitelist_kick_msg': ('You are not whitelisted. Visit https://web.cfrp.co.za to apply.', 'string', 'Kick message when whitelist is off', 'whitelist'),
        'whitelist_closed_msg': ('The server whitelist is currently closed. Check our Discord for updates.', 'string', 'Message shown when whitelist is disabled', 'whitelist'),

2. Add the WhitelistSchedule model below (paste it anywhere after the imports):
"""

from datetime import datetime, timezone
from app.models import db  # already imported in models.py — remove this line when pasting


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
    scheduled_at = db.Column(db.DateTime, nullable=False)         # UTC datetime for next fire
    repeat_type  = db.Column(db.String(16), default='once')       # once / daily / weekly
    is_executed  = db.Column(db.Boolean, default=False)           # for 'once' schedules
    created_by   = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

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
