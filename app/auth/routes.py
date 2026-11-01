import pyotp
import qrcode
import io
import base64
import secrets
from datetime import datetime, timezone, timedelta
from flask import (render_template, redirect, url_for, flash, request,
                   session, jsonify, current_app)
from flask_login import login_user, logout_user, login_required, current_user
from app.auth import auth_bp
from app.models import db, User, Role, AuditLog, Notification


def get_guest_role():
    return Role.query.filter_by(name='guest').first()


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember') == 'on'

        # Find user by username or email
        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier)
        ).first()

        if not user or not user.password_hash or not user.check_password(password):
            if user and not user.password_hash:
                flash('This account uses Discord login. <a href="' + url_for("auth.set_password") + '" style="color:var(--accent)">Set a password here</a> or sign in with Discord.', 'warning')
            else:
                flash('Invalid username/email or password.', 'error')
            return render_template('auth/login.html')

        if user.is_banned:
            flash(f'Your account has been banned. Reason: {user.ban_reason or "No reason given"}', 'error')
            return render_template('auth/login.html')

        if not user.is_active:
            flash('Your account is disabled. Contact an administrator.', 'error')
            return render_template('auth/login.html')

        # Check 2FA
        if user.totp_enabled:
            session['2fa_user_id'] = user.id
            session['2fa_remember'] = remember
            return redirect(url_for('auth.two_factor'))

        _complete_login(user, remember)
        next_page = request.args.get('next')
        return redirect(next_page or url_for('main.index'))

    return render_template('auth/login.html')


@auth_bp.route('/login/2fa', methods=['GET', 'POST'])
def two_factor():
    user_id = session.get('2fa_user_id')
    if not user_id:
        return redirect(url_for('auth.login'))

    user = User.query.get(user_id)
    if not user:
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        code = request.form.get('code', '').replace(' ', '').replace('-', '')

        # Check TOTP
        totp = pyotp.TOTP(user.totp_secret)
        if totp.verify(code, valid_window=1):
            remember = session.pop('2fa_remember', False)
            session.pop('2fa_user_id', None)
            _complete_login(user, remember)
            return redirect(url_for('main.index'))

        # Check backup codes
        backup_codes = user.get_backup_codes()
        if code in backup_codes:
            backup_codes.remove(code)
            user.set_backup_codes(backup_codes)
            db.session.commit()
            remember = session.pop('2fa_remember', False)
            session.pop('2fa_user_id', None)
            _complete_login(user, remember)
            flash('Backup code used. Please generate new backup codes.', 'warning')
            return redirect(url_for('main.index'))

        flash('Invalid authentication code.', 'error')

    return render_template('auth/2fa.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    from app.models import SiteSettings
    if not SiteSettings.get('allow_registration', True):
        flash('Registration is currently closed.', 'warning')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        terms = request.form.get('terms') == 'on'

        errors = []
        if not username or len(username) < 3 or len(username) > 32:
            errors.append('Username must be 3–32 characters.')
        if not email or '@' not in email:
            errors.append('Valid email required.')
        if len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        if password != confirm:
            errors.append('Passwords do not match.')
        if not terms:
            errors.append('You must accept the terms of service.')

        # Check uniqueness
        if User.query.filter_by(username=username).first():
            errors.append('Username already taken.')
        if User.query.filter_by(email=email).first():
            errors.append('Email already registered.')

        # Username character check
        import re
        if username and not re.match(r'^[a-zA-Z0-9_.-]+$', username):
            errors.append('Username may only contain letters, numbers, underscores, dots, and hyphens.')

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('auth/register.html', form_data=request.form)

        # Create user
        user = User(username=username, email=email)
        user.set_password(password)
        user.email_verify_token = secrets.token_urlsafe(32)

        # Assign guest role
        guest_role = get_guest_role()
        if guest_role:
            user.roles.append(guest_role)

        db.session.add(user)
        db.session.commit()

        # Welcome notification
        notif = Notification(
            user_id=user.id,
            title='Welcome to CFRP Whitelist!',
            message='Your account has been created. Please connect your Discord account to apply.',
            type='success',
            link=url_for('discord_oauth.connect'),
        )
        db.session.add(notif)
        db.session.commit()

        AuditLog.log('user.register', user_id=user.id, ip=request.remote_addr)
        db.session.commit()

        login_user(user)
        flash('Account created! Connect your Discord to get started.', 'success')
        return redirect(url_for('discord_oauth.connect'))

    return render_template('auth/register.html')


@auth_bp.route('/register/discord')
def register_discord():
    """Initiate Discord OAuth for signup"""
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    # Store intent so callback knows this is a signup
    session['discord_signup'] = True
    import secrets as _s
    state = _s.token_urlsafe(16)
    session['discord_oauth_state'] = state
    from urllib.parse import urlencode
    params = {
        'client_id': current_app.config['DISCORD_CLIENT_ID'],
        'redirect_uri': current_app.config['DISCORD_REDIRECT_URI'],
        'response_type': 'code',
        'scope': 'identify email guilds.join',
        'state': state,
        'prompt': 'consent',
    }
    return redirect(current_app.config['DISCORD_OAUTH_URL'] + '?' + urlencode(params))


@auth_bp.route('/register/set-password', methods=['GET', 'POST'])
@login_required
def set_password():
    """Step 2 of Discord signup — set a password"""
    # Only show if user has no password yet OR came from Discord signup flow
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        errors = []
        import re
        if not username or len(username) < 3 or len(username) > 32:
            errors.append('Username must be 3–32 characters.')
        if not re.match(r'^[a-zA-Z0-9_.-]+$', username):
            errors.append('Username may only contain letters, numbers, _ . -')
        existing = User.query.filter_by(username=username).first()
        if existing and existing.id != current_user.id:
            errors.append('Username already taken.')
        if len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        if password != confirm:
            errors.append('Passwords do not match.')

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('auth/set_password.html')

        current_user.username = username
        current_user.set_password(password)
        session.pop('discord_signup', None)
        db.session.commit()

        AuditLog.log('user.set_password', user_id=current_user.id, ip=request.remote_addr)
        db.session.commit()

        flash('Password set! Your account is ready.', 'success')
        return redirect(url_for('main.index'))

    return render_template('auth/set_password.html')


@auth_bp.route('/logout')
@login_required
def logout():
    AuditLog.log('user.logout', user_id=current_user.id, ip=request.remote_addr)
    db.session.commit()
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


# ─── 2FA Setup ────────────────────────────────────────────────────────────────

@auth_bp.route('/2fa/setup', methods=['GET', 'POST'])
@login_required
def setup_2fa():
    if current_user.totp_enabled:
        flash('Two-factor authentication is already enabled.', 'info')
        return redirect(url_for('profile.settings'))

    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        secret = session.get('totp_secret_temp')

        if not secret:
            flash('Session expired. Please try again.', 'error')
            return redirect(url_for('auth.setup_2fa'))

        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=1):
            flash('Invalid code. Please try again.', 'error')
            return render_template('auth/setup_2fa.html',
                                   qr_code=session.get('totp_qr'),
                                   secret=secret)

        current_user.totp_secret = secret
        current_user.totp_enabled = True
        backup_codes = current_user.generate_backup_codes()
        session.pop('totp_secret_temp', None)
        session.pop('totp_qr', None)
        db.session.commit()

        AuditLog.log('user.2fa_enabled', user_id=current_user.id, ip=request.remote_addr)
        db.session.commit()

        flash('Two-factor authentication enabled!', 'success')
        return render_template('auth/backup_codes.html', codes=backup_codes)

    # Generate new secret
    secret = pyotp.random_base32()
    session['totp_secret_temp'] = secret

    totp = pyotp.TOTP(secret)
    issuer = current_app.config.get('TOTP_ISSUER', 'CFRP Whitelist')
    uri = totp.provisioning_uri(name=current_user.email, issuer_name=issuer)

    # Generate QR code
    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color='white', back_color='#0f172a')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    session['totp_qr'] = qr_b64

    return render_template('auth/setup_2fa.html', qr_code=qr_b64, secret=secret)


@auth_bp.route('/2fa/disable', methods=['POST'])
@login_required
def disable_2fa():
    password = request.form.get('password', '')
    if not current_user.check_password(password):
        flash('Incorrect password.', 'error')
        return redirect(url_for('profile.settings'))

    current_user.totp_enabled = False
    current_user.totp_secret = None
    current_user.backup_codes = None
    db.session.commit()

    AuditLog.log('user.2fa_disabled', user_id=current_user.id, ip=request.remote_addr)
    db.session.commit()
    flash('Two-factor authentication disabled.', 'success')
    return redirect(url_for('profile.settings'))


# ─── Password Reset ───────────────────────────────────────────────────────────

@auth_bp.route('/password/change', methods=['POST'])
@login_required
def change_password():
    current_pw = request.form.get('current_password', '')
    new_pw = request.form.get('new_password', '')
    confirm_pw = request.form.get('confirm_password', '')

    if not current_user.check_password(current_pw):
        flash('Current password is incorrect.', 'error')
        return redirect(url_for('profile.settings'))

    if len(new_pw) < 8:
        flash('New password must be at least 8 characters.', 'error')
        return redirect(url_for('profile.settings'))

    if new_pw != confirm_pw:
        flash('Passwords do not match.', 'error')
        return redirect(url_for('profile.settings'))

    current_user.set_password(new_pw)
    db.session.commit()
    AuditLog.log('user.password_changed', user_id=current_user.id, ip=request.remote_addr)
    db.session.commit()
    flash('Password changed successfully.', 'success')
    return redirect(url_for('profile.settings'))


# ─── Helper ──────────────────────────────────────────────────────────────────

def _complete_login(user, remember=False):
    login_user(user, remember=remember)
    user.last_login = datetime.now(timezone.utc)
    user.last_ip = request.remote_addr
    user.login_count = (user.login_count or 0) + 1
    db.session.commit()
    AuditLog.log('user.login', user_id=user.id, ip=request.remote_addr)
    db.session.commit()
