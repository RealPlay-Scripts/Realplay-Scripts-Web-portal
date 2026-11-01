"""
app/monitor/__init__.py + routes.py combined into one file.

Add to your app factory (e.g. app/__init__.py):

    from app.monitor.routes import monitor_bp
    app.register_blueprint(monitor_bp)

This blueprint adds:
  POST /api/monitor/frame          — receives screenshot from FiveM client
  GET  /api/monitor/latest         — returns latest frames for all players (JSON)
  GET  /api/monitor/frame/<server_id> — returns raw JPEG for one player
  GET  /admin/monitor              — the web UI page
"""

import time
import threading
from flask import Blueprint, request, jsonify, abort, Response, render_template, current_app
from functools import wraps

monitor_bp = Blueprint('monitor', __name__)

# ──────────────────────────────────────────────────────────────
# In-memory frame store
# { server_id (str) : { 'name': str, 'frame': bytes, 'ts': float } }
# Frames expire after FRAME_TTL seconds (player disconnected / stopped sending)
# ──────────────────────────────────────────────────────────────
_frames: dict = {}
_frames_lock = threading.Lock()
FRAME_TTL = 10  # seconds — if no new frame arrives, slot goes stale


def _require_api_key(f):
    """Decorator — validates X-API-Key header matches BOT_API_KEY in app config."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-API-Key') or request.args.get('api_key')
        expected = current_app.config.get('BOT_API_KEY') or current_app.config.get('API_KEY')
        if not key or key != expected:
            abort(401)
        return f(*args, **kwargs)
    return decorated


def _require_admin_session(f):
    """Decorator — ensures the browser session is an authenticated admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Import here to avoid circular imports
        from flask_login import current_user
        if not current_user.is_authenticated:
            abort(403)
        # Check admin role — adjust to match your User model
        if not getattr(current_user, 'is_admin', False):
            # Try role-based check if your app uses roles
            roles = getattr(current_user, 'roles', [])
            role_names = [getattr(r, 'name', '') for r in roles]
            if 'admin' not in role_names and 'staff' not in role_names:
                abort(403)
        return f(*args, **kwargs)
    return decorated


# ──────────────────────────────────────────────────────────────
# POST /api/monitor/frame
# Called by the FiveM client via screenshot-basic upload
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

    # screenshot-basic posts as multipart with field name "screenshot"
    file = request.files.get('screenshot')
    if not file:
        return jsonify({'error': 'No screenshot field in upload'}), 400

    frame_bytes = file.read()
    if not frame_bytes:
        return jsonify({'error': 'Empty frame'}), 400

    with _frames_lock:
        _frames[server_id] = {
            'name': player_name,
            'frame': frame_bytes,
            'ts': time.time(),
        }

    return jsonify({'ok': True}), 200


# ──────────────────────────────────────────────────────────────
# GET /api/monitor/latest
# Returns JSON list of active players with their last-seen timestamp.
# The web UI polls this to know which players have live feeds.
# ──────────────────────────────────────────────────────────────
@monitor_bp.route('/api/monitor/latest', methods=['GET'])
@_require_admin_session
def latest_players():
    now = time.time()
    result = []
    with _frames_lock:
        for server_id, data in list(_frames.items()):
            age = now - data['ts']
            if age <= FRAME_TTL:
                result.append({
                    'id': server_id,
                    'name': data['name'],
                    'age_ms': int(age * 1000),
                    'ts': data['ts'],
                })
            else:
                # Clean up stale entries
                del _frames[server_id]

    result.sort(key=lambda x: x['name'])
    return jsonify(result)


# ──────────────────────────────────────────────────────────────
# GET /api/monitor/frame/<server_id>
# Returns the raw JPEG for a single player.
# The web UI sets this as the <img src="..."> for each feed cell.
# ──────────────────────────────────────────────────────────────
@monitor_bp.route('/api/monitor/frame/<server_id>', methods=['GET'])
@_require_admin_session
def get_frame(server_id):
    with _frames_lock:
        entry = _frames.get(server_id)

    if not entry:
        abort(404)

    age = time.time() - entry['ts']
    if age > FRAME_TTL:
        with _frames_lock:
            _frames.pop(server_id, None)
        abort(404)

    return Response(
        entry['frame'],
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
