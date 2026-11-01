"""
app/monitor/routes.py — Player Monitor (live screenshot feed)

  POST /api/monitor/frame          — receives a screenshot from the FiveM server
  GET  /api/monitor/latest         — returns latest frame metadata for all players (JSON)
  GET  /api/monitor/frame/<server_id> — returns raw JPEG for one player
  GET  /admin/monitor              — the web UI page

Frames are stored in Redis (REDIS_URL), not in-process memory — the live
deployment runs multiple gunicorn workers, and an in-memory dict would only
be visible to whichever worker happened to receive a given request.
"""

import time
from flask import Blueprint, request, jsonify, abort, Response, render_template, current_app
from functools import wraps

monitor_bp = Blueprint('monitor', __name__)

FRAME_TTL = 10  # seconds — Redis key TTL; if no new frame arrives, the entry expires
_REDIS_PREFIX = 'monitor:frame:'


def _redis():
    """Lazily create and cache a redis client on the app object."""
    if not hasattr(current_app, '_monitor_redis'):
        import redis
        current_app._monitor_redis = redis.from_url(current_app.config.get('REDIS_URL', 'redis://localhost:6379/0'))
    return current_app._monitor_redis


def _require_api_key(f):
    """Decorator — validates X-API-Key header matches MONITOR_API_KEY in app config."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-API-Key') or request.args.get('api_key')
        expected = current_app.config.get('MONITOR_API_KEY')
        if not expected or not key or key != expected:
            abort(401)
        return f(*args, **kwargs)
    return decorated


def _require_admin_session(f):
    """Decorator — ensures the browser session is an authenticated admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask_login import current_user
        if not current_user.is_authenticated or not getattr(current_user, 'is_admin', False):
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ──────────────────────────────────────────────────────────────
# POST /api/monitor/frame
# Called by the FiveM server (rps_playermonitor) after it collects a
# screenshot from a client via screenshot-basic.
# Headers: X-API-Key, X-Player-Token (server ID), X-Player-Name (optional)
# Body: multipart/form-data with field "screenshot" (JPEG bytes)
# ──────────────────────────────────────────────────────────────
@monitor_bp.route('/api/monitor/frame', methods=['POST'])
@_require_api_key
def receive_frame():
    server_id = request.headers.get('X-Player-Token', '').strip()
    player_name = request.headers.get('X-Player-Name', f'Player {server_id}').strip()

    if not server_id:
        return jsonify({'error': 'Missing X-Player-Token'}), 400

    file = request.files.get('screenshot')
    if not file:
        return jsonify({'error': 'No screenshot field in upload'}), 400

    frame_bytes = file.read()
    if not frame_bytes:
        return jsonify({'error': 'Empty frame'}), 400
    if len(frame_bytes) > 5 * 1024 * 1024:
        return jsonify({'error': 'Frame too large'}), 413

    r = _redis()
    key = _REDIS_PREFIX + server_id
    r.hset(key, mapping={'name': player_name, 'ts': time.time(), 'frame': frame_bytes})
    r.expire(key, FRAME_TTL)

    return jsonify({'ok': True}), 200


# ──────────────────────────────────────────────────────────────
# GET /api/monitor/latest
# Returns JSON list of active players with their last-seen timestamp.
# ──────────────────────────────────────────────────────────────
@monitor_bp.route('/api/monitor/latest', methods=['GET'])
@_require_admin_session
def latest_players():
    r = _redis()
    now = time.time()
    result = []
    for key in r.scan_iter(match=_REDIS_PREFIX + '*', count=100):
        server_id = key.decode().removeprefix(_REDIS_PREFIX)
        data = r.hmget(key, 'name', 'ts')
        name, ts = data
        if ts is None:
            continue
        ts = float(ts)
        result.append({
            'id': server_id,
            'name': (name or b'').decode(errors='replace'),
            'age_ms': int((now - ts) * 1000),
            'ts': ts,
        })

    result.sort(key=lambda x: x['name'])
    return jsonify(result)


# ──────────────────────────────────────────────────────────────
# GET /api/monitor/frame/<server_id>
# Returns the raw JPEG for a single player.
# ──────────────────────────────────────────────────────────────
@monitor_bp.route('/api/monitor/frame/<server_id>', methods=['GET'])
@_require_admin_session
def get_frame(server_id):
    r = _redis()
    key = _REDIS_PREFIX + server_id
    data = r.hmget(key, 'frame', 'ts')
    frame, ts = data
    if frame is None:
        abort(404)

    age = time.time() - float(ts)
    return Response(
        frame,
        mimetype='image/jpeg',
        headers={
            'Cache-Control': 'no-store, no-cache, must-revalidate',
            'Pragma': 'no-cache',
            'X-Frame-Age-Ms': str(int(age * 1000)),
        }
    )


# ──────────────────────────────────────────────────────────────
# GET /admin/monitor
# The web UI page
# ──────────────────────────────────────────────────────────────
@monitor_bp.route('/admin/monitor')
@_require_admin_session
def monitor_page():
    return render_template('admin/monitor.html')
