"""
/api/prems  –  In-Game Admin Permissions API
=============================================

Authentication
--------------
All routes require either:
  • X-API-Key: <key>   (header) – for FiveM server / third-party scripts
  • ?api_key=<key>     (query)  – fallback

The FIVEM_API_KEY env var (already used across the platform) is always accepted.
User-generated API keys need the 'prems' scope.

Endpoints
---------
GET  /api/prems/check           Query perm for a player (FiveM → API)
GET  /api/prems/all             Full active perm list (FiveM sync on startup)
POST /api/prems/grant           Grant a perm  (Admin panel → API)
POST /api/prems/revoke/<id>     Revoke a perm (Admin panel → API)
POST /api/prems/update/<id>     Update a perm (Admin panel → API)
GET  /api/prems/list            Paginated list for admin UI
GET  /api/prems/history/<discord_id>  Full history for one player
"""

from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify, current_app
from flask_login import current_user, login_required
from app.models import db, User, InGamePrem, AuditLog
from app.utils import api_key_required

prems_api_bp = Blueprint('prems_api', __name__)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _require_admin_api():
    """Used on write routes that the admin panel calls via JS fetch.
    Accepts both session-authenticated admins and API keys with 'prems' scope."""
    # If called from a logged-in admin session
    if current_user.is_authenticated and current_user.has_permission('admin.access'):
        return None  # OK
    # Otherwise fall through to key check
    return _api_key_check('prems')


def _api_key_check(scope='read'):
    from app.models import APIKey
    key_str = request.headers.get('X-API-Key') or request.args.get('api_key')
    if not key_str:
        return jsonify({'error': 'API key required', 'code': 401}), 401
    if key_str == current_app.config.get('FIVEM_API_KEY'):
        return None  # FiveM server key always accepted
    key = APIKey.query.filter_by(key=key_str, is_active=True).first()
    if not key:
        return jsonify({'error': 'Invalid API key', 'code': 401}), 401
    if key.expires_at and key.expires_at < datetime.now(timezone.utc):
        return jsonify({'error': 'API key expired', 'code': 401}), 401
    if not key.has_scope(scope):
        return jsonify({'error': f'Insufficient scope: {scope} required', 'code': 403}), 403
    key.last_used = datetime.now(timezone.utc)
    key.use_count = (key.use_count or 0) + 1
    db.session.commit()
    return None  # OK


def _audit(action, target, detail=''):
    try:
        actor_id = current_user.id if current_user.is_authenticated else None
        log = AuditLog(
            user_id=actor_id,
            action=action,
            target_type='ingame_prem',
            target_id=str(target),
            detail=detail,
            ip_address=request.remote_addr,
        )
        db.session.add(log)
    except Exception:
        pass  # audit failure must never block the actual operation


# ─── FiveM-facing: check one player ──────────────────────────────────────────

@prems_api_bp.route('/check', methods=['GET'])
@api_key_required('read')
def check_prem():
    """
    Check whether a player has an active in-game perm.

    Query params (at least one required):
      discord_id  –  player's Discord snowflake
      license_id  –  fivem: / steam: / license: identifier

    Returns:
      {
        "has_perm": true,
        "perm_level": "admin",
        "custom_perms": ["command.noclip"],
        "expires_at": null
      }
    """
    discord_id = request.args.get('discord_id')
    license_id = request.args.get('license_id')

    if not discord_id and not license_id:
        return jsonify({'error': 'discord_id or license_id required', 'code': 400}), 400

    query = InGamePrem.query.filter_by(is_active=True)
    if discord_id:
        query = query.filter_by(discord_id=discord_id)
    elif license_id:
        query = query.filter_by(license_id=license_id)

    prem = query.order_by(InGamePrem.granted_at.desc()).first()

    if not prem or prem.is_expired:
        return jsonify({'has_perm': False, 'perm_level': None, 'custom_perms': [], 'expires_at': None})

    return jsonify({
        'has_perm':     True,
        'perm_level':   prem.perm_level,
        'custom_perms': prem.get_custom_perms(),
        'expires_at':   prem.expires_at.isoformat() if prem.expires_at else None,
        'player_name':  prem.player_name,
        'discord_id':   prem.discord_id,
    })


# ─── FiveM-facing: full list for startup sync ─────────────────────────────────

@prems_api_bp.route('/all', methods=['GET'])
@api_key_required('read')
def all_prems():
    """
    Return all currently-active, non-expired perms.
    FiveM script calls this on resource start to pre-load ace perms.

    Returns:
      {
        "count": 3,
        "prems": [
          {
            "discord_id": "...",
            "license_id": "...",
            "perm_level": "admin",
            "custom_perms": []
          },
          ...
        ]
      }
    """
    now = datetime.now(timezone.utc)
    prems = InGamePrem.query.filter_by(is_active=True).filter(
        db.or_(InGamePrem.expires_at == None, InGamePrem.expires_at > now)
    ).all()

    return jsonify({
        'count': len(prems),
        'prems': [
            {
                'discord_id':   p.discord_id,
                'license_id':   p.license_id,
                'perm_level':   p.perm_level,
                'custom_perms': p.get_custom_perms(),
                'player_name':  p.player_name,
            }
            for p in prems
        ]
    })


# ─── Admin panel: paginated list ──────────────────────────────────────────────

@prems_api_bp.route('/list', methods=['GET'])
@login_required
def list_prems():
    """Admin-facing paginated list. Requires admin session."""
    if not current_user.has_permission('admin.access'):
        return jsonify({'error': 'Forbidden', 'code': 403}), 403

    page     = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    active   = request.args.get('active')    # 'true' / 'false' / omit for all
    search   = request.args.get('q', '').strip()

    query = InGamePrem.query
    if active == 'true':
        query = query.filter_by(is_active=True)
    elif active == 'false':
        query = query.filter_by(is_active=False)
    if search:
        like = f'%{search}%'
        query = query.filter(
            db.or_(
                InGamePrem.discord_id.ilike(like),
                InGamePrem.player_name.ilike(like),
                InGamePrem.license_id.ilike(like),
            )
        )

    pagination = query.order_by(InGamePrem.granted_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    now = datetime.now(timezone.utc)
    items = []
    for p in pagination.items:
        d = p.to_dict()
        d['is_expired'] = bool(p.expires_at and p.expires_at < now)
        items.append(d)

    return jsonify({
        'prems':      items,
        'total':      pagination.total,
        'page':       page,
        'per_page':   per_page,
        'pages':      pagination.pages,
    })


# ─── Admin panel: grant ───────────────────────────────────────────────────────

@prems_api_bp.route('/grant', methods=['POST'])
@login_required
def grant_prem():
    """
    Grant a new in-game perm.

    Body (JSON):
      discord_id   required
      perm_level   required  (moderator / admin / superadmin / owner)
      license_id   optional
      player_name  optional
      custom_perms optional  list of strings
      note         optional
      expires_in_days  optional  int (omit or 0 = permanent)
    """
    if not current_user.has_permission('admin.access'):
        return jsonify({'error': 'Forbidden', 'code': 403}), 403

    data       = request.get_json(silent=True) or {}
    discord_id = data.get('discord_id', '').strip()
    perm_level = data.get('perm_level', '').strip()

    if not discord_id:
        return jsonify({'error': 'discord_id required', 'code': 400}), 400
    if perm_level not in InGamePrem.PERM_LEVELS:
        return jsonify({'error': f'perm_level must be one of {InGamePrem.PERM_LEVELS}', 'code': 400}), 400

    expires_at = None
    days = data.get('expires_in_days')
    if days:
        try:
            expires_at = datetime.now(timezone.utc) + timedelta(days=int(days))
        except (ValueError, TypeError):
            return jsonify({'error': 'expires_in_days must be an integer', 'code': 400}), 400

    # Deactivate any existing active perm for this discord_id so there's only one
    InGamePrem.query.filter_by(discord_id=discord_id, is_active=True).update({'is_active': False})

    prem = InGamePrem(
        discord_id   = discord_id,
        player_name  = data.get('player_name', '').strip() or None,
        license_id   = data.get('license_id', '').strip() or None,
        perm_level   = perm_level,
        note         = data.get('note', '').strip() or None,
        granted_by   = current_user.id,
        expires_at   = expires_at,
    )
    prem.set_custom_perms(data.get('custom_perms', []))
    db.session.add(prem)
    db.session.flush()

    _audit('grant_ingame_prem', prem.id, f'{discord_id} → {perm_level}')
    db.session.commit()

    return jsonify({'success': True, 'prem': prem.to_dict()}), 201


# ─── Admin panel: revoke ──────────────────────────────────────────────────────

@prems_api_bp.route('/revoke/<int:prem_id>', methods=['POST'])
@login_required
def revoke_prem(prem_id):
    """Revoke (soft-delete) a perm by ID."""
    if not current_user.has_permission('admin.access'):
        return jsonify({'error': 'Forbidden', 'code': 403}), 403

    prem = InGamePrem.query.get_or_404(prem_id)
    prem.is_active  = False
    prem.revoked_by = current_user.id
    prem.revoked_at = datetime.now(timezone.utc)

    _audit('revoke_ingame_prem', prem.id, f'{prem.discord_id} [{prem.perm_level}]')
    db.session.commit()

    return jsonify({'success': True, 'prem': prem.to_dict()})


# ─── Admin panel: update ──────────────────────────────────────────────────────

@prems_api_bp.route('/update/<int:prem_id>', methods=['POST'])
@login_required
def update_prem(prem_id):
    """
    Update an existing perm record.
    Accepts same fields as grant (except discord_id which is immutable).
    """
    if not current_user.has_permission('admin.access'):
        return jsonify({'error': 'Forbidden', 'code': 403}), 403

    prem = InGamePrem.query.get_or_404(prem_id)
    data = request.get_json(silent=True) or {}

    if 'perm_level' in data:
        if data['perm_level'] not in InGamePrem.PERM_LEVELS:
            return jsonify({'error': f'perm_level must be one of {InGamePrem.PERM_LEVELS}', 'code': 400}), 400
        prem.perm_level = data['perm_level']

    if 'player_name' in data:
        prem.player_name = data['player_name'].strip() or None
    if 'license_id' in data:
        prem.license_id  = data['license_id'].strip() or None
    if 'note' in data:
        prem.note        = data['note'].strip() or None
    if 'custom_perms' in data:
        prem.set_custom_perms(data['custom_perms'])
    if 'is_active' in data:
        prem.is_active = bool(data['is_active'])
    if 'expires_in_days' in data:
        days = data['expires_in_days']
        prem.expires_at = (
            datetime.now(timezone.utc) + timedelta(days=int(days)) if days else None
        )

    _audit('update_ingame_prem', prem.id, f'{prem.discord_id} [{prem.perm_level}]')
    db.session.commit()

    return jsonify({'success': True, 'prem': prem.to_dict()})


# ─── History for one player ───────────────────────────────────────────────────

@prems_api_bp.route('/history/<discord_id>', methods=['GET'])
@login_required
def prem_history(discord_id):
    """Full perm history (all records, including revoked) for a given discord_id."""
    if not current_user.has_permission('admin.access'):
        return jsonify({'error': 'Forbidden', 'code': 403}), 403

    records = InGamePrem.query.filter_by(discord_id=discord_id)\
        .order_by(InGamePrem.granted_at.desc()).all()

    return jsonify({
        'discord_id': discord_id,
        'history':    [p.to_dict() for p in records],
    })
