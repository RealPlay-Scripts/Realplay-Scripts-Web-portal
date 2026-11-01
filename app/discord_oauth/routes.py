import secrets
import requests
from datetime import datetime, timezone, timedelta
from flask import (redirect, url_for, session, request, flash,
                   current_app, render_template, jsonify)
from flask_login import login_required, current_user
from app.discord_oauth import discord_bp
from app.models import db, User, AuditLog, Notification
from app.utils import add_discord_role, remove_discord_role, get_discord_member


@discord_bp.route('/connect')
def connect():
    """Initiate Discord OAuth2 flow (linking, not signup)"""
    if not current_user.is_authenticated:
        return redirect(url_for('auth.register_discord'))

    state = secrets.token_urlsafe(16)
    session['discord_oauth_state'] = state

    params = {
        'client_id': current_app.config['DISCORD_CLIENT_ID'],
        'redirect_uri': current_app.config['DISCORD_REDIRECT_URI'],
        'response_type': 'code',
        'scope': 'identify email guilds.join',
        'state': state,
        'prompt': 'consent',
    }
    from urllib.parse import urlencode
    auth_url = current_app.config['DISCORD_OAUTH_URL'] + '?' + urlencode(params)
    return redirect(auth_url)


@discord_bp.route('/callback')
def callback():
    """Handle Discord OAuth2 callback"""
    state = request.args.get('state')
    code = request.args.get('code')
    error = request.args.get('error')

    if error:
        flash(f'Discord authorization failed: {error}', 'error')
        return redirect(url_for('auth.login'))

    if state != session.get('discord_oauth_state'):
        flash('Invalid OAuth state. Please try again.', 'error')
        return redirect(url_for('auth.login'))

    session.pop('discord_oauth_state', None)

    if not code:
        flash('No authorization code received.', 'error')
        return redirect(url_for('auth.login'))

    # Exchange code for token
    token_data = _exchange_code(code)
    if not token_data:
        flash('Failed to get Discord token. Please try again.', 'error')
        return redirect(url_for('auth.login'))

    access_token = token_data.get('access_token')
    refresh_token = token_data.get('refresh_token')
    expires_in = token_data.get('expires_in', 604800)

    # Fetch Discord user
    discord_user = _get_discord_user(access_token)
    if not discord_user:
        flash('Failed to fetch Discord profile.', 'error')
        return redirect(url_for('auth.login'))

    discord_id = discord_user.get('id')
    discord_username = discord_user.get('username')
    discord_avatar = discord_user.get('avatar')
    discord_email = discord_user.get('email', '')

    # ── SIGNUP FLOW ────────────────────────────────────────────────────────
    is_signup = session.pop('discord_signup', False)

    if not current_user.is_authenticated:
        # Check if Discord ID already linked to an existing account → just log them in
        existing = User.query.filter_by(discord_id=discord_id).first()

        # No discord_id match — check by email before falling through to signup/error
        if not existing and discord_email:
            existing = User.query.filter_by(email=discord_email).first()
            if existing:
                # Link this Discord to the account that shares the email
                existing.discord_id       = discord_id
                existing.discord_verified = True
                _do_discord_token_update(existing, access_token, refresh_token,
                                         expires_in, discord_username, discord_avatar)
                db.session.commit()
                _join_guild(discord_id, access_token)
                from app.auth.routes import _complete_login
                _complete_login(existing, remember=True)
                flash(f'Discord linked to your account. Welcome back, {existing.username}!', 'success')
                return redirect(url_for('main.index'))

        if existing and not is_signup:
            # Login flow
            _do_discord_token_update(existing, access_token, refresh_token, expires_in, discord_username, discord_avatar)
            db.session.commit()
            _join_guild(discord_id, access_token)
            from app.auth.routes import _complete_login
            _complete_login(existing, remember=True)
            flash(f'Welcome back, {existing.username}!', 'success')
            return redirect(url_for('main.index'))

        if existing and is_signup:
            # They clicked signup but already have an account → log them in
            _do_discord_token_update(existing, access_token, refresh_token, expires_in, discord_username, discord_avatar)
            db.session.commit()
            from app.auth.routes import _complete_login
            _complete_login(existing, remember=True)
            flash(f'You already have an account. Welcome back, {existing.username}!', 'info')
            return redirect(url_for('main.index'))

        if is_signup:
            # New account via Discord signup flow
            base_username = discord_username or f'user_{discord_id[:6]}'
            import re
            base_username = re.sub(r'[^a-zA-Z0-9_.-]', '_', base_username)[:28]
            username = base_username
            counter = 1
            while User.query.filter_by(username=username).first():
                username = f'{base_username}{counter}'
                counter += 1

            # If an account with this email already exists, link Discord to it
            # instead of creating a duplicate and crashing on the UNIQUE constraint.
            if discord_email:
                email_match = User.query.filter_by(email=discord_email).first()
                if email_match:
                    _do_discord_token_update(email_match, access_token, refresh_token,
                                             expires_in, discord_username, discord_avatar)
                    email_match.discord_id       = discord_id
                    email_match.discord_verified = True
                    db.session.commit()
                    _join_guild(discord_id, access_token)
                    from app.auth.routes import _complete_login
                    _complete_login(email_match, remember=True)
                    flash('Your Discord has been linked to your existing account. Welcome back!', 'success')
                    return redirect(url_for('main.index'))

            user = User(
                username=username,
                email=discord_email or f'{discord_id}@discord.placeholder',
                discord_id=discord_id,
                discord_username=discord_username,
                discord_avatar=discord_avatar,
                discord_access_token=access_token,
                discord_refresh_token=refresh_token,
                discord_token_expires=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                discord_verified=True,
                is_active=True,
            )
            user.password_hash = ''

            from app.models import Role, Notification
            guest_role = Role.query.filter_by(name='guest').first()
            if guest_role:
                user.roles.append(guest_role)

            db.session.add(user)
            db.session.commit()
            _join_guild(discord_id, access_token)

            from app.auth.routes import _complete_login
            _complete_login(user, remember=True)

            notif = Notification(
                user_id=user.id,
                title='Welcome to CFRP Whitelist!',
                message='Your Discord account is linked. Set a password to secure your account.',
                type='success',
            )
            db.session.add(notif)
            db.session.commit()

            flash('Discord connected! Please set a password for your account.', 'success')
            return redirect(url_for('auth.set_password'))

        # Not logged in, not signup, no existing account → send to register
        flash('No account found for that Discord. Please sign up first.', 'warning')
        return redirect(url_for('auth.register'))

    # ── EXISTING USER LINKING FLOW (authenticated user adding Discord) ────
    if not current_user.is_authenticated:
        flash('Please log in first.', 'warning')
        return redirect(url_for('auth.login'))

    # Check if Discord account already linked to another user
    existing = User.query.filter_by(discord_id=discord_id).first()
    if existing and existing.id != current_user.id:
        flash('This Discord account is already linked to another user.', 'error')
        return redirect(url_for('profile.settings'))

    _do_discord_token_update(current_user, access_token, refresh_token, expires_in, discord_username, discord_avatar)
    db.session.commit()

    _join_guild(discord_id, access_token)

    AuditLog.log('user.discord_connected', user_id=current_user.id,
                 details={'discord_id': discord_id, 'username': discord_username},
                 ip=request.remote_addr)

    from app.models import Notification
    notif = Notification(
        user_id=current_user.id,
        title='Discord Connected',
        message=f'Your Discord account ({discord_username}) has been linked successfully.',
        type='success',
    )
    db.session.add(notif)
    db.session.commit()

    flash(f'Discord account {discord_username} connected!', 'success')
    return redirect(url_for('profile.view', username=current_user.username))


@discord_bp.route('/disconnect', methods=['POST'])
@login_required
def disconnect():
    """Disconnect Discord account"""
    current_user.discord_id = None
    current_user.discord_username = None
    current_user.discord_avatar = None
    current_user.discord_access_token = None
    current_user.discord_refresh_token = None
    current_user.discord_token_expires = None
    current_user.discord_verified = False
    db.session.commit()

    AuditLog.log('user.discord_disconnected', user_id=current_user.id, ip=request.remote_addr)
    db.session.commit()
    flash('Discord account disconnected.', 'info')
    return redirect(url_for('profile.settings'))


@discord_bp.route('/sync', methods=['POST'])
@login_required
def sync_roles():
    """Sync Discord roles based on site roles"""
    if not current_user.discord_id:
        flash('No Discord account connected.', 'error')
        return redirect(url_for('profile.settings'))

    synced = _sync_user_roles(current_user)
    if synced:
        flash('Discord roles synced successfully.', 'success')
    else:
        flash('Role sync failed. Check bot permissions.', 'warning')
    return redirect(url_for('profile.settings'))


# ─── Admin Discord Sync ───────────────────────────────────────────────────────

@discord_bp.route('/admin/sync/<int:user_id>', methods=['POST'])
@login_required
def admin_sync_user(user_id):
    if not current_user.is_admin:
        return jsonify({'error': 'Forbidden'}), 403

    user = User.query.get_or_404(user_id)
    if not user.discord_id:
        return jsonify({'success': False, 'message': 'User has no Discord account linked'}), 400

    result = _sync_user_roles_detailed(user)
    return jsonify(result)


@discord_bp.route('/admin/sync-all', methods=['POST'])
@login_required
def admin_sync_all():
    if not current_user.is_admin:
        return jsonify({'error': 'Forbidden'}), 403

    users = User.query.filter(User.discord_id.isnot(None)).all()
    success_count = 0
    for user in users:
        r = _sync_user_roles_detailed(user)
        if r.get('success'):
            success_count += 1

    return jsonify({'success': True, 'synced': success_count, 'total': len(users)})


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _do_discord_token_update(user, access_token, refresh_token, expires_in, username, avatar):
    user.discord_access_token = access_token
    user.discord_refresh_token = refresh_token
    user.discord_token_expires = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    user.discord_username = username
    user.discord_avatar = avatar
    user.discord_verified = True


def _exchange_code(code):
    data = {
        'client_id': current_app.config['DISCORD_CLIENT_ID'],
        'client_secret': current_app.config['DISCORD_CLIENT_SECRET'],
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': current_app.config['DISCORD_REDIRECT_URI'],
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    try:
        resp = requests.post(current_app.config['DISCORD_TOKEN_URL'],
                             data=data, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        current_app.logger.error(f'Token exchange failed: {resp.status_code} {resp.text}')
    except requests.RequestException as e:
        current_app.logger.error(f'Token exchange error: {e}')
    return None


def _get_discord_user(access_token):
    headers = {'Authorization': f'Bearer {access_token}'}
    try:
        resp = requests.get(f"{current_app.config['DISCORD_API_BASE']}/users/@me",
                            headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException as e:
        current_app.logger.error(f'Discord user fetch error: {e}')
    return None


def _join_guild(discord_id, access_token):
    """Add user to the Discord guild via bot"""
    guild_id = current_app.config['DISCORD_GUILD_ID']
    bot_token = current_app.config['DISCORD_BOT_TOKEN']
    if not bot_token:
        return False
    headers = {
        'Authorization': f'Bot {bot_token}',
        'Content-Type': 'application/json',
    }
    payload = {'access_token': access_token}
    try:
        resp = requests.put(
            f"{current_app.config['DISCORD_API_BASE']}/guilds/{guild_id}/members/{discord_id}",
            json=payload, headers=headers, timeout=10
        )
        return resp.status_code in (200, 201, 204)
    except requests.RequestException:
        return False


def _sync_user_roles_detailed(user) -> dict:
    """
    Sync site roles → Discord roles with full add + remove logic.
    Returns a dict with success, message, and details of what changed.
    """
    if not user.discord_id:
        return {'success': False, 'message': 'No Discord account linked to this user'}

    guild_id  = current_app.config.get('DISCORD_GUILD_ID', '')
    bot_token = current_app.config.get('DISCORD_BOT_TOKEN', '')

    if not bot_token:
        return {'success': False, 'message': 'DISCORD_BOT_TOKEN is not set in .env'}
    if not guild_id:
        return {'success': False, 'message': 'DISCORD_GUILD_ID is not set in .env'}

    from app.utils import discord_api_request, add_discord_role, remove_discord_role

    # Fetch the member from Discord
    resp = discord_api_request('GET', f'/guilds/{guild_id}/members/{user.discord_id}')
    if resp is None:
        return {'success': False, 'message': 'Network error reaching Discord API'}
    if resp.status_code == 404:
        return {'success': False, 'message': f'User `{user.discord_id}` is not in the Discord server (not a member)'}
    if resp.status_code == 401:
        return {'success': False, 'message': 'Bot token is invalid or expired — check DISCORD_BOT_TOKEN'}
    if resp.status_code == 403:
        return {'success': False, 'message': 'Bot lacks permission to view guild members — needs the Members Intent'}
    if resp.status_code != 200:
        return {'success': False, 'message': f'Discord API returned HTTP {resp.status_code}: {resp.text[:200]}'}

    member                = resp.json()
    current_discord_roles = set(member.get('roles', []))

    # Which Discord role IDs should this user have (from site roles)
    should_have = set()
    for role in user.roles:
        if role.discord_role_id:
            should_have.add(role.discord_role_id)

    # All Discord role IDs managed by any site role (to know which ones to remove)
    from app.models import Role
    all_managed = set(
        r.discord_role_id for r in Role.query.filter(Role.discord_role_id.isnot(None)).all()
    )

    to_add    = should_have - current_discord_roles
    to_remove = (all_managed & current_discord_roles) - should_have  # only remove roles we manage

    added   = []
    removed = []
    errors  = []

    for role_id in to_add:
        r = discord_api_request('PUT', f'/guilds/{guild_id}/members/{user.discord_id}/roles/{role_id}')
        if r and r.status_code == 204:
            added.append(role_id)
        else:
            status = r.status_code if r else 'network error'
            errors.append(f'Failed to add role {role_id}: HTTP {status}')

    for role_id in to_remove:
        r = discord_api_request('DELETE', f'/guilds/{guild_id}/members/{user.discord_id}/roles/{role_id}')
        if r and r.status_code == 204:
            removed.append(role_id)
        else:
            status = r.status_code if r else 'network error'
            errors.append(f'Failed to remove role {role_id}: HTTP {status}')

    if not to_add and not to_remove:
        msg = 'Already up to date — no changes needed'
    else:
        parts = []
        if added:   parts.append(f'{len(added)} role(s) added')
        if removed: parts.append(f'{len(removed)} role(s) removed')
        if errors:  parts.append(f'{len(errors)} error(s)')
        msg = ', '.join(parts)

    return {
        'success':    len(errors) == 0,
        'message':    msg,
        'added':      added,
        'removed':    removed,
        'errors':     errors,
        'no_changes': not to_add and not to_remove,
    }


def _sync_user_roles(user) -> bool:
    """Legacy wrapper — returns bool for backwards compat."""
    return _sync_user_roles_detailed(user).get('success', False)



    if not user.discord_id:
        return False

    guild_id = current_app.config['DISCORD_GUILD_ID']
    bot_token = current_app.config['DISCORD_BOT_TOKEN']
    if not bot_token or not guild_id:
        return False

    from app.utils import discord_api_request

    # Get all roles with Discord role IDs
    roles_to_add = []
    for role in user.roles:
        if role.discord_role_id:
            roles_to_add.append(role.discord_role_id)

    try:
        # Get current member roles
        member = get_discord_member(user.discord_id, guild_id)
        if not member:
            return False

        current_discord_roles = member.get('roles', [])

        # Add missing roles
        for role_id in roles_to_add:
            if role_id not in current_discord_roles:
                add_discord_role(user.discord_id, role_id, guild_id)

        return True
    except Exception as e:
        current_app.logger.error(f'Role sync error for {user.username}: {e}')
        return False
