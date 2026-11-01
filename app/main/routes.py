from flask import render_template, redirect, url_for
from flask_login import current_user
from app.main import main_bp
from app.models import ApplicationType, PlayerHeartbeat, PlayerStat, SiteSettings
from app.utils import format_duration
from datetime import datetime, timezone, timedelta


@main_bp.route('/')
def index():
    # Live player count
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=90)
    live_count = PlayerHeartbeat.query.filter(PlayerHeartbeat.last_seen >= cutoff).count()

    # Total kills this week
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    kills_week = PlayerStat.query.filter(
        PlayerStat.event_type == 'kill',
        PlayerStat.recorded_at >= week_ago
    ).count()

    # Active application types
    app_types = ApplicationType.query.filter_by(is_active=True).order_by(ApplicationType.sort_order).all()

    return render_template('index.html',
        live_count=live_count,
        kills_week=kills_week,
        app_types=app_types,
    )


@main_bp.route('/status')
def status():
    return render_template('status.html')
