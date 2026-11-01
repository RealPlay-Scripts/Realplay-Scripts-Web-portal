import os
import hashlib
import hmac
import json
import requests
import functools
from datetime import datetime, timezone
from flask import current_app, request, jsonify, abort
from flask_login import current_user
from werkzeug.utils import secure_filename


# ─── Auth Decorators ──────────────────────────────────────────────────────────

def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def permission_required(perm):
    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated or not current_user.has_permission(perm):
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


def api_key_required(scope='read'):
    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            from app.models import APIKey
            from datetime import datetime, timezone
            auth_header = request.headers.get('Authorization', '').strip()
            if auth_header.startswith('Bearer '):
                key_str = auth_header[len('Bearer '):].strip()
            elif auth_header.startswith('ApiKey '):
                key_str = auth_header[len('ApiKey '):].strip()
            else:
                key_str = request.headers.get('X-API-Key') or request.args.get('api_key')

            if not key_str:
                return jsonify({'error': 'API key required', 'code': 401}), 401
            # Check FiveM server key
            if key_str == current_app.config.get('FIVEM_API_KEY'):
                return f(*args, **kwargs)
            key = APIKey.query.filter_by(key=key_str, is_active=True).first()
            if not key:
                return jsonify({'error': 'Invalid API key', 'code': 401}), 401
            if key.expires_at and key.expires_at < datetime.now(timezone.utc):
                return jsonify({'error': 'API key expired', 'code': 401}), 401
            if not key.has_scope(scope):
                return jsonify({'error': f'Insufficient scope, requires: {scope}', 'code': 403}), 403
            key.last_used = datetime.now(timezone.utc)
            key.use_count = (key.use_count or 0) + 1
            from app.models import db
            db.session.commit()
            return f(*args, **kwargs)
        return decorated
    return decorator


# ─── File Utilities ────────────────────────────────────────────────────────────

def allowed_file(filename):
    allowed = current_app.config.get('ALLOWED_EXTENSIONS', {'png', 'jpg', 'jpeg', 'gif', 'webp'})
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


def save_upload(file, subfolder='general', prefix=''):
    if not file or not allowed_file(file.filename):
        return None
    filename = secure_filename(file.filename)
    ext = filename.rsplit('.', 1)[1].lower()
    unique_name = f"{prefix}{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex()}.{ext}"
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], subfolder)
    os.makedirs(folder, exist_ok=True)
    filepath = os.path.join(folder, unique_name)
    file.save(filepath)
    return f"/static/uploads/{subfolder}/{unique_name}"


# ─── Discord Utilities ────────────────────────────────────────────────────────

def discord_api_request(method, endpoint, **kwargs):
    base = current_app.config['DISCORD_API_BASE']
    token = current_app.config['DISCORD_BOT_TOKEN']
    headers = {
        'Authorization': f'Bot {token}',
        'Content-Type': 'application/json',
    }
    headers.update(kwargs.pop('headers', {}))
    url = f"{base}{endpoint}"
    try:
        resp = requests.request(method, url, headers=headers, timeout=10, **kwargs)
        return resp
    except requests.RequestException as e:
        current_app.logger.error(f"Discord API error: {e}")
        return None


def add_discord_role(discord_id, role_id, guild_id=None):
    guild = guild_id or current_app.config['DISCORD_GUILD_ID']
    resp = discord_api_request('PUT', f'/guilds/{guild}/members/{discord_id}/roles/{role_id}')
    return resp and resp.status_code == 204


def remove_discord_role(discord_id, role_id, guild_id=None):
    guild = guild_id or current_app.config['DISCORD_GUILD_ID']
    resp = discord_api_request('DELETE', f'/guilds/{guild}/members/{discord_id}/roles/{role_id}')
    return resp and resp.status_code == 204


def get_discord_member(discord_id, guild_id=None):
    guild = guild_id or current_app.config['DISCORD_GUILD_ID']
    resp = discord_api_request('GET', f'/guilds/{guild}/members/{discord_id}')
    if resp and resp.status_code == 200:
        return resp.json()
    return None


def get_discord_guild_roles(guild_id=None):
    guild = guild_id or current_app.config['DISCORD_GUILD_ID']
    resp = discord_api_request('GET', f'/guilds/{guild}/roles')
    if resp and resp.status_code == 200:
        return resp.json()
    return []


# ─── Webhook Delivery ─────────────────────────────────────────────────────────

def send_webhook(webhook, application, event):
    import time
    from app.models import db, WebhookDelivery
    from app.models import User

    embed_cfg = webhook.get_embed_config()
    user = application.applicant
    app_type = application.application_type

    color_map = {
        'submit': 0x6366f1,
        'approve': 0x22c55e,
        'deny': 0xef4444,
        'review': 0x3b82f6,
        'hold': 0xf59e0b,
    }
    embed_color = color_map.get(event, 0x6366f1)

    status_labels = {
        'submit': '📋 New Application',
        'approve': '✅ Application Approved',
        'deny': '❌ Application Denied',
        'review': '🔍 Under Review',
        'hold': '⏸️ On Hold',
    }

    embed = {
        'title': status_labels.get(event, f'Application {event.title()}'),
        'color': embed_cfg.get('color', embed_color),
        'fields': [
            {'name': 'Applicant', 'value': user.username, 'inline': True},
            {'name': 'Application Type', 'value': app_type.name, 'inline': True},
            {'name': 'Status', 'value': application.status.replace('_', ' ').title(), 'inline': True},
            {'name': 'Application ID', 'value': f'#{application.id}', 'inline': True},
        ],
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'footer': {'text': 'GSRP Whitelist System'},
    }

    if user.discord_id:
        embed['fields'].append({'name': 'Discord', 'value': f'<@{user.discord_id}>', 'inline': True})

    if application.review_note:
        embed['fields'].append({'name': 'Note', 'value': application.review_note[:1024], 'inline': False})

    if embed_cfg.get('thumbnail'):
        embed['thumbnail'] = {'url': embed_cfg['thumbnail']}

    payload = {
        'username': embed_cfg.get('username', 'CFRP Whitelist'),
        'avatar_url': embed_cfg.get('avatar_url', ''),
        'embeds': [embed],
    }

    start = time.time()
    delivery = WebhookDelivery(
        webhook_id=webhook.id,
        application_id=application.id,
        event=event,
        payload=json.dumps(payload),
    )

    try:
        resp = requests.post(webhook.url, json=payload, timeout=10)
        duration = int((time.time() - start) * 1000)
        delivery.status_code = resp.status_code
        delivery.response_body = resp.text[:512]
        delivery.success = resp.status_code in (200, 204)
        delivery.duration_ms = duration
        webhook.last_status_code = resp.status_code
        webhook.last_triggered = datetime.now(timezone.utc)
        webhook.trigger_count = (webhook.trigger_count or 0) + 1
    except requests.RequestException as e:
        delivery.success = False
        delivery.last_error = str(e)
        webhook.last_error = str(e)

    db.session.add(delivery)
    db.session.commit()
    return delivery.success


def trigger_webhooks(application, event):
    from app.models import Webhook
    webhooks = Webhook.query.filter_by(type_id=application.type_id, is_active=True).all()
    trigger_map = {
        'submit': 'on_submit',
        'approve': 'on_approve',
        'deny': 'on_deny',
        'review': 'on_review',
        'hold': 'on_hold',
    }
    attr = trigger_map.get(event)
    for wh in webhooks:
        if attr and getattr(wh, attr, False):
            send_webhook(wh, application, event)


# ─── Pagination Helper ────────────────────────────────────────────────────────

def paginate_query(query, page=1, per_page=20):
    return query.paginate(page=page, per_page=per_page, error_out=False)


# ─── Time Formatting ──────────────────────────────────────────────────────────

def format_duration(seconds):
    if not seconds:
        return '0m'
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h > 0:
        return f'{h}h {m}m'
    return f'{m}m'


def format_duration_long(seconds):
    if not seconds:
        return '0 minutes'
    days = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    parts = []
    if days > 0:
        parts.append(f'{days}d')
    if h > 0:
        parts.append(f'{h}h')
    if m > 0 or not parts:
        parts.append(f'{m}m')
    return ' '.join(parts)


def time_ago(dt):
    if not dt:
        return 'never'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    diff = now - dt
    s = diff.total_seconds()
    if s < 60:
        return 'just now'
    if s < 3600:
        return f'{int(s//60)}m ago'
    if s < 86400:
        return f'{int(s//3600)}h ago'
    return f'{int(s//86400)}d ago'


# ── send_discord_dm ───────────────────────────────────────────────────────────

def send_discord_dm(discord_id, *, content=None, embeds=None):
    if not discord_id:
        return False

    channel_resp = discord_api_request(
        'POST',
        '/users/@me/channels',
        json={'recipient_id': str(discord_id)},
    )
    if channel_resp is None or not channel_resp.ok:
        current_app.logger.warning(
            f'send_discord_dm: failed to open DM channel for {discord_id} '
            f'— {getattr(channel_resp, "status_code", "no response")}'
        )
        return False

    channel_id = channel_resp.json().get('id')
    if not channel_id:
        current_app.logger.warning(f'send_discord_dm: no channel id returned for {discord_id}')
        return False

    payload = {}
    if content:
        payload['content'] = content
    if embeds:
        payload['embeds'] = embeds

    if not payload:
        current_app.logger.warning('send_discord_dm: called with no content or embeds')
        return False

    msg_resp = discord_api_request(
        'POST',
        f'/channels/{channel_id}/messages',
        json=payload,
    )
    if msg_resp is None or not msg_resp.ok:
        current_app.logger.warning(
            f'send_discord_dm: message send failed for {discord_id} '
            f'— {getattr(msg_resp, "status_code", "no response")} '
            f'{getattr(msg_resp, "text", "")[:200]}'
        )
        return False

    return True
