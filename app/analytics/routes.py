from datetime import datetime, timezone, timedelta
from flask import render_template, jsonify, request, abort
from flask_login import login_required, current_user
from app.analytics import analytics_bp
from app.models import db, PlayerSession, PlayerStat, PlayerHeartbeat, PlaytimeAggregate, AuditLog
from app.utils import format_duration, format_duration_long, time_ago


def _admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def _week_ago():
    return datetime.now(timezone.utc) - timedelta(days=7)


def _today_start():
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


@analytics_bp.route('/')
@login_required
@_admin_required
def dashboard():
    return render_template('analytics/dashboard.html')


@analytics_bp.route('/crashes')
@login_required
@_admin_required
def crashes():
    return render_template('analytics/crashes.html')


@analytics_bp.route('/economy')
@login_required
@_admin_required
def economy():
    return render_template('analytics/economy.html')


# ─── AJAX Data Endpoints ──────────────────────────────────────────────────────

@analytics_bp.route('/data/overview')
def data_overview():
    now = datetime.now(timezone.utc)
    today = _today_start()
    week_ago = now - timedelta(days=7)

    cutoff = now - timedelta(seconds=90)
    live_count = PlayerHeartbeat.query.filter(PlayerHeartbeat.last_seen >= cutoff).count()

    live_players = PlayerHeartbeat.query.filter(PlayerHeartbeat.last_seen >= cutoff).all()

    playtime_today = db.session.query(db.func.sum(PlayerSession.duration_seconds))\
        .filter(PlayerSession.join_time >= today).scalar() or 0
    playtime_week = db.session.query(db.func.sum(PlayerSession.duration_seconds))\
        .filter(PlayerSession.join_time >= week_ago).scalar() or 0

    kills_week = PlayerStat.query.filter(
        PlayerStat.event_type == 'kill', PlayerStat.recorded_at >= week_ago).count()
    deaths_week = PlayerStat.query.filter(
        PlayerStat.event_type == 'death', PlayerStat.recorded_at >= week_ago).count()

    unique_players = db.session.query(
        db.func.count(db.func.distinct(PlayerSession.discord_id))).scalar() or 0

    # Recent killfeed
    recent_kills = PlayerStat.query.filter_by(event_type='kill')\
        .order_by(PlayerStat.recorded_at.desc()).limit(10).all()

    return jsonify({
        'live_count': live_count,
        'live_players': [
            {'name': p.player_name, 'discord_id': p.discord_id,
             'cash': p.cash, 'bank': p.bank, 'last_seen': p.last_seen.isoformat()}
            for p in live_players
        ],
        'playtime_today': format_duration(playtime_today),
        'playtime_today_raw': playtime_today,
        'playtime_week': format_duration(playtime_week),
        'playtime_week_raw': playtime_week,
        'kills_week': kills_week,
        'deaths_week': deaths_week,
        'kd_week': round(kills_week / max(deaths_week, 1), 2),
        'unique_players': unique_players,
        'killfeed': [
            {
                'killer': k.killer_name or 'Unknown',
                'victim': k.victim_name or 'Unknown',
                'weapon': k.weapon or 'Unknown',
                'cause': k.cause,
                'ago': time_ago(k.recorded_at),
            }
            for k in recent_kills
        ],
    })


@analytics_bp.route('/data/leaderboard')
def data_leaderboard():
    period = request.args.get('period', 'weekly')
    now = datetime.now(timezone.utc)
    cutoffs = {
        'daily': now - timedelta(days=1),
        'weekly': now - timedelta(days=7),
        'monthly': now - timedelta(days=30),
        'yearly': now - timedelta(days=365),
    }
    cutoff = cutoffs.get(period, cutoffs['weekly'])

    rows = db.session.query(
        PlayerSession.discord_id,
        PlayerSession.player_name,
        db.func.sum(PlayerSession.duration_seconds).label('total')
    ).filter(
        PlayerSession.join_time >= cutoff,
        PlayerSession.duration_seconds.isnot(None)
    ).group_by(PlayerSession.discord_id, PlayerSession.player_name)\
     .order_by(db.func.sum(PlayerSession.duration_seconds).desc())\
     .limit(25).all()

    return jsonify({
        'period': period,
        'leaderboard': [
            {
                'rank': i + 1,
                'discord_id': r.discord_id,
                'name': r.player_name or 'Unknown',
                'seconds': r.total,
                'formatted': format_duration(r.total),
            }
            for i, r in enumerate(rows)
        ]
    })


@analytics_bp.route('/data/peak-hours')
def data_peak_hours():
    """Hourly player count over last 7 days"""
    week_ago = _week_ago()
    sessions = PlayerSession.query.filter(
        PlayerSession.join_time >= week_ago
    ).all()

    hourly = [0] * 24
    for s in sessions:
        hour = s.join_time.hour
        hourly[hour] += 1

    return jsonify({
        'labels': [f'{h:02d}:00' for h in range(24)],
        'data': hourly,
        'peak_hour': hourly.index(max(hourly)) if any(hourly) else 0,
        'peak_count': max(hourly) if any(hourly) else 0,
    })


@analytics_bp.route('/data/retention')
def data_retention():
    """Player retention: sessions per player over last 30 days"""
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    rows = db.session.query(
        PlayerSession.discord_id,
        db.func.count(PlayerSession.id).label('sessions'),
        db.func.sum(PlayerSession.duration_seconds).label('total_time'),
    ).filter(
        PlayerSession.join_time >= month_ago
    ).group_by(PlayerSession.discord_id)\
     .order_by(db.func.count(PlayerSession.id).desc()).limit(20).all()

    buckets = {'1': 0, '2-5': 0, '6-10': 0, '11-20': 0, '20+': 0}
    for r in rows:
        n = r.sessions
        if n == 1: buckets['1'] += 1
        elif n <= 5: buckets['2-5'] += 1
        elif n <= 10: buckets['6-10'] += 1
        elif n <= 20: buckets['11-20'] += 1
        else: buckets['20+'] += 1

    return jsonify({
        'buckets': buckets,
        'top_players': [
            {'discord_id': r.discord_id, 'sessions': r.sessions,
             'total_time': format_duration(r.total_time or 0)}
            for r in rows[:10]
        ]
    })


@analytics_bp.route('/data/crash-summary')
def data_crash_summary():
    seven_days = _week_ago()
    now = datetime.now(timezone.utc)

    crashes  = PlayerSession.query.filter(PlayerSession.join_time >= seven_days, PlayerSession.disconnect_category == 'crash').count()
    timeouts = PlayerSession.query.filter(PlayerSession.join_time >= seven_days, PlayerSession.disconnect_category == 'timeout').count()
    network  = PlayerSession.query.filter(PlayerSession.join_time >= seven_days, PlayerSession.disconnect_category == 'connection').count()

    avg_ping = db.session.query(db.func.avg(PlayerSession.disconnect_ping)).filter(
        PlayerSession.join_time >= seven_days,
        PlayerSession.disconnect_category.in_(['crash', 'timeout']),
        PlayerSession.disconnect_ping.isnot(None),
        PlayerSession.disconnect_ping > 0
    ).scalar() or 0

    ping_query = PlayerSession.query.filter(
        PlayerSession.join_time >= seven_days,
        PlayerSession.disconnect_category.in_(['crash', 'timeout']),
        PlayerSession.disconnect_ping.isnot(None),
        PlayerSession.disconnect_ping > 0
    )
    ping_good = ping_query.filter(PlayerSession.disconnect_ping < 80).count()
    ping_fair = ping_query.filter(PlayerSession.disconnect_ping.between(80, 150)).count()
    ping_poor = ping_query.filter(PlayerSession.disconnect_ping.between(150, 300)).count()
    ping_crit = ping_query.filter(PlayerSession.disconnect_ping > 300).count()

    # 14-day daily trend split by category
    daily_trend = []
    for i in range(14):
        day = now - timedelta(days=13 - i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day.replace(hour=23, minute=59, second=59)
        c = PlayerSession.query.filter(PlayerSession.join_time >= day_start, PlayerSession.join_time <= day_end, PlayerSession.disconnect_category == 'crash').count()
        t = PlayerSession.query.filter(PlayerSession.join_time >= day_start, PlayerSession.join_time <= day_end, PlayerSession.disconnect_category == 'timeout').count()
        daily_trend.append({'day': day.strftime('%a %d'), 'crashes': c, 'timeouts': t})

    # Hourly hotspots
    hourly_crashes  = [0] * 24
    hourly_timeouts = [0] * 24
    for s in PlayerSession.query.filter(PlayerSession.join_time >= seven_days, PlayerSession.disconnect_category == 'crash').all():
        hourly_crashes[s.join_time.hour] += 1
    for s in PlayerSession.query.filter(PlayerSession.join_time >= seven_days, PlayerSession.disconnect_category == 'timeout').all():
        hourly_timeouts[s.join_time.hour] += 1

    # Root cause breakdown
    categories = db.session.query(
        PlayerSession.disconnect_category,
        db.func.count(PlayerSession.id).label('count')
    ).filter(
        PlayerSession.join_time >= seven_days,
        PlayerSession.disconnect_category.isnot(None)
    ).group_by(PlayerSession.disconnect_category)\
     .order_by(db.func.count(PlayerSession.id).desc()).all()

    # Repeat crash players (crashes only)
    repeat_crash = db.session.query(
        PlayerSession.discord_id,
        PlayerSession.player_name,
        db.func.count(PlayerSession.id).label('crash_count'),
        db.func.avg(PlayerSession.disconnect_ping).label('avg_ping'),
        db.func.avg(PlayerSession.duration_seconds).label('avg_session'),
    ).filter(
        PlayerSession.join_time >= seven_days,
        PlayerSession.disconnect_category == 'crash',
    ).group_by(PlayerSession.discord_id, PlayerSession.player_name)\
     .having(db.func.count(PlayerSession.id) >= 2)\
     .order_by(db.func.count(PlayerSession.id).desc()).limit(10).all()

    # Repeat timeout players (separate)
    repeat_timeout = db.session.query(
        PlayerSession.discord_id,
        PlayerSession.player_name,
        db.func.count(PlayerSession.id).label('timeout_count'),
        db.func.avg(PlayerSession.disconnect_ping).label('avg_ping'),
        db.func.avg(PlayerSession.duration_seconds).label('avg_session'),
    ).filter(
        PlayerSession.join_time >= seven_days,
        PlayerSession.disconnect_category == 'timeout',
    ).group_by(PlayerSession.discord_id, PlayerSession.player_name)\
     .having(db.func.count(PlayerSession.id) >= 2)\
     .order_by(db.func.count(PlayerSession.id).desc()).limit(10).all()

    # Timeout reason text analysis — group the raw disconnect_reason strings
    timeout_reasons = db.session.query(
        PlayerSession.disconnect_reason,
        db.func.count(PlayerSession.id).label('count')
    ).filter(
        PlayerSession.join_time >= seven_days,
        PlayerSession.disconnect_category == 'timeout',
        PlayerSession.disconnect_reason.isnot(None),
        PlayerSession.disconnect_reason != '',
    ).group_by(PlayerSession.disconnect_reason)\
     .order_by(db.func.count(PlayerSession.id).desc()).limit(15).all()

    # Session duration at crash — bucket into short/medium/long
    def _dur_buckets(cat):
        sessions = PlayerSession.query.filter(
            PlayerSession.join_time >= seven_days,
            PlayerSession.disconnect_category == cat,
            PlayerSession.duration_seconds.isnot(None),
        ).with_entities(PlayerSession.duration_seconds).all()
        short  = sum(1 for (s,) in sessions if s <  300)   # < 5 min
        medium = sum(1 for (s,) in sessions if 300 <= s < 1800)  # 5–30 min
        long_  = sum(1 for (s,) in sessions if s >= 1800)  # > 30 min
        avg    = sum(s for (s,) in sessions) / max(len(sessions), 1)
        return {'short': short, 'medium': medium, 'long': long_, 'avg_seconds': round(avg)}

    crash_dur   = _dur_buckets('crash')
    timeout_dur = _dur_buckets('timeout')

    # Recent individual incidents (last 50 crashes + timeouts)
    recent_incidents = PlayerSession.query.filter(
        PlayerSession.disconnect_category.in_(['crash', 'timeout']),
    ).order_by(PlayerSession.join_time.desc()).limit(50).all()

    # Timeout diagnosis signals
    high_ping_timeouts   = ping_query.filter(PlayerSession.disconnect_category == 'timeout', PlayerSession.disconnect_ping > 200).count()
    short_session_crashes = PlayerSession.query.filter(
        PlayerSession.join_time >= seven_days,
        PlayerSession.disconnect_category == 'crash',
        PlayerSession.duration_seconds < 120,
    ).count()

    total_issues = crashes + timeouts + network

    # ── Deep reason-string pattern matching ───────────────────────────────────
    # Pull all disconnect reasons for the period so we can classify them
    all_reasons = PlayerSession.query.filter(
        PlayerSession.join_time >= seven_days,
        PlayerSession.disconnect_reason.isnot(None),
        PlayerSession.disconnect_reason != '',
    ).with_entities(
        PlayerSession.disconnect_reason,
        PlayerSession.disconnect_category,
        PlayerSession.disconnect_ping,
        PlayerSession.duration_seconds,
    ).all()

    # Known FiveM/server reason signatures → (label, diagnosis, fix steps)
    REASON_PATTERNS = [
        # Timeout / connection
        ('Connection timed out',       'connection_timeout',
         'Connection Timeout',
         'The server stopped receiving heartbeats from the client.',
         ['Check `sv_timeout` in server.cfg (default 30s — try 60)',
          'Look for blocking `Wait(0)` loops in Lua threads',
          'Check if a resource is sending large reliable-channel events (e.g. big ox_lib notifications)',
          'Use `netstat` to verify server outbound bandwidth is not saturated']),
        ('Timed out',                  'connection_timeout',
         'Generic Timeout',
         'Client exceeded the server timeout window.',
         ['Raise `sv_timeout` in server.cfg',
          'Check for CPU-intensive resources (Chromium-based NUI, heavy threads)',
          'Monitor `txAdmin > Server > Resources` for CPU % spikes']),
        ('Server->client flow control', 'flow_control',
         'Flow Control Kick',
         'Server sent data faster than the client could receive — usually a resource sending a burst of large events.',
         ['Search all resources for `TriggerClientEvent` inside `for` loops with no `Wait()`',
          'Compress large table payloads before sending (use `msgpack` or split into batches)',
          'Check qb-core/ox_inventory item syncs — large inventories are a common cause']),
        ('client->server flow',        'flow_control',
         'Client→Server Flow Control',
         'Client was sending data faster than the server could process.',
         ['Look for `TriggerServerEvent` calls in resource `Tick` handlers',
          'Check NUI callbacks that fire on every frame',
          'Throttle client→server events with a cooldown or debounce']),
        ('Disconnected',               'client_disconnect',
         'Normal Disconnect',
         'Player disconnected normally (quit, kicked, or crash exit detected as clean).',
         ['No action needed unless count is unexpectedly high',
          'Cross-reference with crash logs if a specific player appears repeatedly']),
        ('Steam',                      'steam_error',
         'Steam Auth Failure',
         'Steam ticket validation failed — player may have been offline or ticket expired.',
         ['Ensure `steam_webApiKey` is set in server.cfg',
          'This can also happen if a player loses internet briefly mid-session',
          'Check https://store.steampowered.com/status for Steam outages']),
        ('EAC',                        'eac_kick',
         'EasyAntiCheat Kick',
         'EAC flagged the player or detected an integrity issue.',
         ['Player may have modified game files — advise verifying GTA V integrity via Steam/Epic/R*',
          'Ensure EAC is enabled and up-to-date on server (`ensure monitor` in server.cfg)',
          'Persistent EAC kicks for same player = likely cheating tools']),
        ('Kicked',                     'admin_kick',
         'Admin / Rule Kick',
         'Player was manually kicked by a staff member or automated rule.',
         ['Review txAdmin action logs for the kick reason',
          'If automated (e.g. AFK kick), check the resource config for false positives']),
        ('quit',                       'player_quit',
         'Player Quit',
         'Player chose to leave via the pause menu.',
         ['Normal — no action needed']),
        ('server shutting down',       'server_restart',
         'Server Restart',
         'Session ended because the server restarted or stopped.',
         ['Normal if planned — check txAdmin scheduled restarts',
          'If unplanned, check server logs for out-of-memory or crash exit']),
        ('no active',                  'no_license',
         'No Active License / Identifier',
         'Player connected without a valid Rockstar/Steam license.',
         ['Check `identifiers` in server.cfg — `steam` and `license` must be enabled',
          'Player may be using a cracked copy — reject with a clear message']),
        ('model streaming',            'streaming_crash',
         'Model Streaming Failure',
         'Client failed to stream a model or asset, causing a crash.',
         ['Check for invalid prop hashes in resource stream folders',
          'Validate all `.ydr/.yft/.ymap` files with CodeWalker',
          'Look for models with >65535 drawables or missing collision files']),
        ('assert',                     'script_assert',
         'Script Assertion / Lua Error',
         'A Lua assert() failed or a script threw an unhandled error at crash time.',
         ['Enable `set sv_scriptHookAllowed 0` if not already',
          'Check F8 console logs on client — the assert message will name the resource',
          'Review recent resource updates — assertion errors often follow a bad update']),
        ('infinite loop',              'script_loop',
         'Infinite Loop / Hang',
         'A Lua script entered an infinite loop, freezing the game thread.',
         ['Search resources for `while true do` without `Wait()`',
          'Use `citizen:setThreadIdentifier` for long-running threads so you can profile them',
          'Check ESX/QB framework version for known loop bugs']),
        ('memory',                     'oom',
         'Out of Memory / RAM',
         'Client ran out of memory — usually caused by streaming too many assets.',
         ['Reduce the number of simultaneously loaded DLC packs',
          'Check for texture memory leaks in custom MLOs (large textures, wrong mipmap settings)',
          'Use FPS-limiter mods to reduce GPU pressure which can ease VRAM pressure']),
        ('native',                     'native_crash',
         'Native / Engine Crash',
         'A game native function threw an exception — usually a null entity or invalid argument.',
         ['Check any resource using `GetEntityCoords`, `GetPedBone`, or similar on null entities',
          'Wrap native calls with `if DoesEntityExist(ent) then` guards',
          'Use `AddEventHandler("gameEventTriggered", ...)` to catch entity-deleted events']),
    ]

    def _classify_reasons(reasons_list):
        """
        Match each reason string against known patterns.
        Returns a list of classified issues with counts and fix steps.
        """
        buckets = {}  # label → {count, diagnosis, fix_steps, examples}
        for reason, category, ping, duration in reasons_list:
            if not reason:
                continue
            reason_lower = reason.lower()
            matched = False
            for keyword, key, label, diagnosis, fix_steps in REASON_PATTERNS:
                if keyword.lower() in reason_lower:
                    if key not in buckets:
                        buckets[key] = {'label': label, 'diagnosis': diagnosis,
                                        'fix_steps': fix_steps, 'count': 0,
                                        'examples': [], 'categories': set(),
                                        'avg_ping': [], 'avg_duration': []}
                    buckets[key]['count'] += 1
                    buckets[key]['categories'].add(category or 'unknown')
                    if ping and ping > 0:
                        buckets[key]['avg_ping'].append(ping)
                    if duration and duration > 0:
                        buckets[key]['avg_duration'].append(duration)
                    if len(buckets[key]['examples']) < 3:
                        buckets[key]['examples'].append(reason[:120])
                    matched = True
                    break
            if not matched:
                key = 'unclassified'
                if key not in buckets:
                    buckets[key] = {'label': 'Unclassified Disconnect',
                                    'diagnosis': 'Reason string does not match any known FiveM error pattern.',
                                    'fix_steps': ['Copy the reason string and search the cfx.re forum / FiveM Discord',
                                                  'Check txAdmin logs for additional context around this time'],
                                    'count': 0, 'examples': [], 'categories': set(),
                                    'avg_ping': [], 'avg_duration': []}
                buckets[key]['count'] += 1
                buckets[key]['categories'].add(category or 'unknown')
                if len(buckets[key]['examples']) < 5:
                    buckets[key]['examples'].append(reason[:120])

        result = []
        for key, b in sorted(buckets.items(), key=lambda x: -x[1]['count']):
            result.append({
                'key':         key,
                'label':       b['label'],
                'diagnosis':   b['diagnosis'],
                'fix_steps':   b['fix_steps'],
                'count':       b['count'],
                'categories':  list(b['categories']),
                'avg_ping':    round(sum(b['avg_ping']) / max(len(b['avg_ping']), 1), 1),
                'avg_duration_seconds': round(sum(b['avg_duration']) / max(len(b['avg_duration']), 1)),
                'examples':    b['examples'],
            })
        return result

    classified_reasons = _classify_reasons(all_reasons)

    # ── Threshold-based suggestions (keep these for overall picture) ──────────
    suggestions = []
    if total_issues > 0:
        crash_pct   = crashes  / max(total_issues, 1)
        timeout_pct = timeouts / max(total_issues, 1)
        net_pct     = network  / max(total_issues, 1)

        # Server-health-level suggestions
        if crash_pct > 0.4:
            steps = ['Check F8 / CitizenFX.log on a crashing client',
                     'Use `restart <resource>` to isolate the problem resource',
                     'Enable `set lua_enable_profiler 1` and connect with Chromium DevTools']
            if crash_dur['short'] > crash_dur['long']:
                steps.insert(0, 'Majority of crashes are under 5 min — focus on load/streaming errors first')
            suggestions.append({'priority': 'high', 'issue': 'High crash rate — server-side action required',
                                 'steps': steps, 'count': crashes})

        if timeout_pct > 0.3:
            if high_ping_timeouts > timeouts * 0.6:
                steps = ['Most timeouts have high ping — this is a network issue not a script issue',
                         'Add `set sv_timeout 60` to server.cfg (current FiveM default is 30s)',
                         'Investigate players on cellular/satellite connections',
                         'Check if your hosting provider has DDoS mitigation that may be dropping UDP']
            else:
                steps = ['Timeouts with normal ping = server-side blocking',
                         'Run `profiler record 500` in server console then open the trace in chrome://tracing',
                         'Identify which resource thread is blocking — look for `Wait(0)` abuse',
                         'Check ox_lib, qb-core, or ESX schedulers for runaway tasks']
            suggestions.append({'priority': 'high', 'issue': 'High timeout rate',
                                 'steps': steps, 'count': timeouts})

        if high_ping_timeouts > 0:
            suggestions.append({
                'priority': 'high',
                'issue': f'{high_ping_timeouts} timeouts caused by high ping (>200ms)',
                'steps': [
                    'Add `set sv_timeout 60` to server.cfg to give high-ping players more leeway',
                    'Add `set sv_maxPing 350` to auto-kick players whose ping is too high to sustain a session (prevents repeated timeouts)',
                    'Check if any resource is sending large reliable-channel events on every tick',
                    'Consider using `onesync` legacy mode if not already, which handles high-latency clients better',
                ],
                'count': high_ping_timeouts,
            })

        if short_session_crashes > 0:
            suggestions.append({
                'priority': 'medium',
                'issue': f'{short_session_crashes} crashes within 2 min of joining',
                'steps': [
                    'These happen during resource streaming or client initialisation',
                    'Check for models with invalid hashes: search resources for `.ydr`/`.yft` files and validate them',
                    'Review fxmanifest.lua in recently updated resources — missing `file` entries cause streaming errors',
                    'Disable custom map MLOs one at a time to isolate a bad interior',
                    'Check `CitizenFX.log` on the crashing PC — the last 10 lines before the crash will name the asset',
                ],
                'count': short_session_crashes,
            })

        if ping_crit > 0:
            suggestions.append({
                'priority': 'medium',
                'issue': f'{ping_crit} sessions with critical ping at crash (>300ms)',
                'steps': [
                    f'These {ping_crit} sessions had ping >300ms — likely not a server bug',
                    'Ping >300ms consistently = player ISP or geographic distance issue',
                    'Use txAdmin to check if these are the same players each time',
                    'Consider adding a max ping rule: `set sv_maxPing 300` in server.cfg',
                ],
                'count': ping_crit,
            })

        if net_pct > 0.25:
            suggestions.append({
                'priority': 'medium',
                'issue': 'High network drop rate',
                'steps': [
                    'Identify which resources send the most events: use `neteventlog` in server console',
                    'Look for `TriggerNetworkEvent` inside loops — batch these into single events',
                    'Check ox_inventory or qb-inventory for large item-table syncs',
                    'Review custom HUD resources for per-frame event calls',
                ],
                'count': network,
            })

        if repeat_timeout:
            suggestions.append({
                'priority': 'low',
                'issue': f'{len(repeat_timeout)} players with repeated timeouts',
                'steps': [
                    'View the Timeout Players table above to identify them',
                    'Message each player: ask them to run a speed test and check their ping',
                    'Common causes: WiFi instead of ethernet, VPN, ISP throttling UDP',
                    'If their avg ping is <100ms and still timing out, the issue is server-side for them specifically — check if a particular resource fails for their client',
                ],
                'count': len(repeat_timeout),
            })

        if repeat_crash:
            suggestions.append({
                'priority': 'low',
                'issue': f'{len(repeat_crash)} players with repeated crashes',
                'steps': [
                    'View the Crash Players table above',
                    'Ask each player to send their CitizenFX.log (Documents/Rockstar Games/GTA V/CitizenFX.log)',
                    'Common causes: mod menu residue, outdated ScriptHookV, corrupted game files',
                    'Ask them to verify GTA V files via Steam / Epic / Rockstar Launcher',
                    'If the crash is reproducible, ask them to disable all mods and reconnect',
                ],
                'count': len(repeat_crash),
            })

    return jsonify({
        'crashes_7d':        crashes,
        'timeouts_7d':       timeouts,
        'network_drops_7d':  network,
        'total_incidents':   total_issues,
        'avg_ping_at_crash': round(float(avg_ping), 1),
        'ping_health': {'good': ping_good, 'fair': ping_fair, 'poor': ping_poor, 'critical': ping_crit},
        'daily_trend':       daily_trend,
        'hourly_crashes':    hourly_crashes,
        'hourly_timeouts':   hourly_timeouts,
        'root_causes':       [{'category': r.disconnect_category, 'count': r.count} for r in categories],
        'crash_duration':    crash_dur,
        'timeout_duration':  timeout_dur,
        'timeout_reasons':   [{'reason': r.disconnect_reason, 'count': r.count} for r in timeout_reasons],
        'classified_reasons': classified_reasons,
        'repeat_crash_players': [
            {'discord_id': r.discord_id, 'name': r.player_name or 'Unknown',
             'crash_count': r.crash_count, 'avg_ping': round(float(r.avg_ping or 0), 1),
             'avg_session': format_duration(int(r.avg_session or 0))}
            for r in repeat_crash
        ],
        'repeat_timeout_players': [
            {'discord_id': r.discord_id, 'name': r.player_name or 'Unknown',
             'timeout_count': r.timeout_count, 'avg_ping': round(float(r.avg_ping or 0), 1),
             'avg_session': format_duration(int(r.avg_session or 0))}
            for r in repeat_timeout
        ],
        'recent_incidents': [
            {'player': s.player_name or 'Unknown', 'discord_id': s.discord_id,
             'category': s.disconnect_category, 'reason': s.disconnect_reason or '',
             'ping': s.disconnect_ping or 0, 'duration': format_duration(s.duration_seconds),
             'duration_seconds': s.duration_seconds or 0,
             'time': s.join_time.strftime('%d %b %H:%M')}
            for s in recent_incidents
        ],
        'suggestions': sorted(suggestions, key=lambda x: {'high': 0, 'medium': 1, 'low': 2}[x['priority']]),
        'server_health': 'good' if total_issues < 10 else ('warning' if total_issues < 30 else 'critical'),
    })


    """Economy overview from latest heartbeats"""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=90)
    players = PlayerHeartbeat.query.filter(PlayerHeartbeat.last_seen >= cutoff).all()

    total_cash = sum(p.cash or 0 for p in players)
    total_bank = sum(p.bank or 0 for p in players)
    avg_cash = total_cash // max(len(players), 1)
    avg_bank = total_bank // max(len(players), 1)

    richest = sorted(players, key=lambda p: (p.cash or 0) + (p.bank or 0), reverse=True)[:5]

    return jsonify({
        'online_players': len(players),
        'total_cash_in_game': total_cash,
        'total_bank_in_game': total_bank,
        'avg_cash_per_player': avg_cash,
        'avg_bank_per_player': avg_bank,
        'richest_players': [
            {'name': p.player_name, 'cash': p.cash, 'bank': p.bank,
             'total': (p.cash or 0) + (p.bank or 0)}
            for p in richest
        ],
    })


@analytics_bp.route('/data/session-logs')
def data_session_logs():
    page = request.args.get('page', 1, type=int)
    category = request.args.get('category', '')
    query = PlayerSession.query
    if category:
        query = query.filter_by(disconnect_category=category)
    pagination = query.order_by(PlayerSession.join_time.desc()).paginate(page=page, per_page=30, error_out=False)
    return jsonify({
        'sessions': [
            {
                'id': s.id,
                'discord_id': s.discord_id,
                'player_name': s.player_name,
                'join_time': s.join_time.isoformat(),
                'leave_time': s.leave_time.isoformat() if s.leave_time else None,
                'duration': format_duration(s.duration_seconds),
                'duration_seconds': s.duration_seconds,
                'category': s.disconnect_category,
                'reason': s.disconnect_reason,
                'ping': s.disconnect_ping,
                'is_online': s.leave_time is None,
            }
            for s in pagination.items
        ],
        'total': pagination.total,
        'pages': pagination.pages,
        'page': page,
    })


@analytics_bp.route('/data/economy')
def data_economy():
    """Live economy snapshot from heartbeats + historical wealth from session endings."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=90)
    week_ago = now - timedelta(days=7)
    today = _today_start()

    # ── Live players ──────────────────────────────────────────────────────────
    live_players = PlayerHeartbeat.query.filter(PlayerHeartbeat.last_seen >= cutoff).all()

    total_cash = sum(p.cash or 0 for p in live_players)
    total_bank = sum(p.bank or 0 for p in live_players)
    avg_cash   = total_cash // max(len(live_players), 1)
    avg_bank   = total_bank // max(len(live_players), 1)

    richest_live = sorted(live_players, key=lambda p: (p.cash or 0) + (p.bank or 0), reverse=True)[:10]

    # ── Historical wealth from sessions (cash_at_leave / bank_at_leave) ───────
    # All-time richest (by cash_at_leave snapshot) — one row per discord_id
    historical = db.session.query(
        PlayerSession.discord_id,
        PlayerSession.player_name,
        db.func.max(PlayerSession.cash_at_leave).label('peak_cash'),
        db.func.max(PlayerSession.bank_at_leave).label('peak_bank'),
        db.func.count(PlayerSession.id).label('sessions'),
    ).filter(
        PlayerSession.cash_at_leave.isnot(None),
    ).group_by(PlayerSession.discord_id, PlayerSession.player_name)\
     .order_by((db.func.max(PlayerSession.cash_at_leave) + db.func.max(PlayerSession.bank_at_leave)).desc())\
     .limit(20).all()

    # ── Economy trend — avg cash+bank at session end per day (last 14 days) ──
    daily_wealth = []
    for i in range(14):
        day = now - timedelta(days=13 - i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day.replace(hour=23, minute=59, second=59)
        row = db.session.query(
            db.func.avg(PlayerSession.cash_at_leave + PlayerSession.bank_at_leave).label('avg_total'),
            db.func.sum(PlayerSession.cash_at_leave + PlayerSession.bank_at_leave).label('sum_total'),
            db.func.count(PlayerSession.id).label('sessions'),
        ).filter(
            PlayerSession.leave_time >= day_start,
            PlayerSession.leave_time <= day_end,
            PlayerSession.cash_at_leave.isnot(None),
        ).first()
        daily_wealth.append({
            'day': day.strftime('%a %d'),
            'avg_total': round(float(row.avg_total or 0)),
            'sum_total': int(row.sum_total or 0),
            'sessions':  int(row.sessions or 0),
        })

    # ── Cash distribution buckets (from live heartbeats) ─────────────────────
    broke     = sum(1 for p in live_players if (p.cash or 0) < 1000)
    low       = sum(1 for p in live_players if 1000 <= (p.cash or 0) < 10000)
    mid       = sum(1 for p in live_players if 10000 <= (p.cash or 0) < 100000)
    high      = sum(1 for p in live_players if 100000 <= (p.cash or 0) < 500000)
    whale     = sum(1 for p in live_players if (p.cash or 0) >= 500000)

    # ── Session economy change — cash gained/lost per session this week ───────
    sessions_week = PlayerSession.query.filter(
        PlayerSession.leave_time >= week_ago,
        PlayerSession.cash_at_leave.isnot(None),
    ).with_entities(
        PlayerSession.discord_id,
        PlayerSession.player_name,
        PlayerSession.cash_at_leave,
        PlayerSession.bank_at_leave,
        PlayerSession.duration_seconds,
    ).all()

    total_cash_circulating_week  = sum((s.cash_at_leave or 0) for s in sessions_week)
    total_bank_circulating_week  = sum((s.bank_at_leave or 0) for s in sessions_week)
    avg_cash_leave_week = total_cash_circulating_week // max(len(sessions_week), 1)
    avg_bank_leave_week = total_bank_circulating_week // max(len(sessions_week), 1)

    # Most active earners this week (highest cash_at_leave sessions)
    top_earners = sorted(sessions_week, key=lambda s: (s.cash_at_leave or 0), reverse=True)[:5]

    return jsonify({
        # Live snapshot
        'online_players':        len(live_players),
        'total_cash_in_game':    total_cash,
        'total_bank_in_game':    total_bank,
        'total_wealth_in_game':  total_cash + total_bank,
        'avg_cash_per_player':   avg_cash,
        'avg_bank_per_player':   avg_bank,
        # Live leaderboard
        'richest_players': [
            {'name':  p.player_name or 'Unknown',
             'cash':  p.cash  or 0,
             'bank':  p.bank  or 0,
             'total': (p.cash or 0) + (p.bank or 0)}
            for p in richest_live
        ],
        # Historical leaderboard (all time, by peak wealth at session end)
        'historical_richest': [
            {'discord_id': r.discord_id,
             'name':       r.player_name or 'Unknown',
             'peak_cash':  r.peak_cash  or 0,
             'peak_bank':  r.peak_bank  or 0,
             'peak_total': (r.peak_cash or 0) + (r.peak_bank or 0),
             'sessions':   r.sessions}
            for r in historical
        ],
        # Trend
        'daily_wealth': daily_wealth,
        # Distribution
        'cash_distribution': {
            'broke': broke,     # < $1k
            'low':   low,       # $1k–$10k
            'mid':   mid,       # $10k–$100k
            'high':  high,      # $100k–$500k
            'whale': whale,     # $500k+
        },
        # Weekly session stats
        'week_sessions':          len(sessions_week),
        'avg_cash_leave_week':    avg_cash_leave_week,
        'avg_bank_leave_week':    avg_bank_leave_week,
        'top_earners_week': [
            {'name': s.player_name or 'Unknown',
             'cash': s.cash_at_leave or 0,
             'bank': s.bank_at_leave or 0}
            for s in top_earners
        ],
    })


# ─── Economy Flag Detection ────────────────────────────────────────────────────

FLAG_THRESHOLD = 50_000   # $ gain in 24h that triggers a flag
ALERT_STEP     = 50_000   # additional gain required after review to re-alert
_monitor_started = False  # guard so we only start one thread


# ── Discord webhook helpers ───────────────────────────────────────────────────

def _webhook_url():
    try:
        from flask import current_app
        return current_app.config.get('ECONOMY_FLAGS_WEBHOOK', '')
    except RuntimeError:
        return ''


def _build_embed(player_name, discord_id, gain, cash_before, bank_before,
                 cash_after, bank_after, detected_at=None, session_start=None,
                 prev_gain=None, update_count=0, change_log=None):
    is_update    = prev_gain is not None
    now          = datetime.now(timezone.utc)
    total_before = cash_before + bank_before
    total_after  = cash_after  + bank_after
    gain_diff    = gain - prev_gain if prev_gain is not None else 0

    if detected_at:
        mins     = int((now - detected_at).total_seconds() / 60)
        hours    = mins // 60
        active_str = f"{hours}h {mins % 60}m" if hours else f"{mins}m"
        secs     = max((now - detected_at).total_seconds(), 1)
        rate_str = f"${int(gain / secs * 3600):,}/hr"
    else:
        active_str = "Just now"
        rate_str   = "—"

    session_str  = f"<t:{int(session_start.timestamp())}:t>" if session_start else "Unknown"
    detected_str = f"<t:{int(detected_at.timestamp())}:f>"   if detected_at   else "Now"

    title  = "🔄 Economy Flag — Updated"      if is_update else "⚠️ Economy Flag — New Detection"
    colour = 0xF59E0B                          if is_update else 0xEF4444

    diff_line = ""
    if is_update and gain_diff > 0:
        diff_line = f"\n📈 Up **+${gain_diff:,}** since last check."
    elif is_update and gain_diff < 0:
        diff_line = f"\n📉 Down **${gain_diff:,}** since last check."

    description = (
        f"**{player_name}** has gained **${gain:,}** in the last 24 hours."
        + diff_line
    )

    fields = [
        {"name": "👤 Player",        "value": f"**{player_name}**\n`{discord_id}`",                                           "inline": True},
        {"name": "⏱ Active",         "value": active_str,                                                                       "inline": True},
        {"name": "💹 Rate",           "value": rate_str,                                                                         "inline": True},
        {"name": "📉 Wealth Before",  "value": f"💵 Cash: `${cash_before:,}`\n🏦 Bank: `${bank_before:,}`\n📊 Total: `${total_before:,}`", "inline": True},
        {"name": "📈 Wealth Now",     "value": f"💵 Cash: `${cash_after:,}`\n🏦 Bank: `${bank_after:,}`\n📊 Total: `${total_after:,}`",    "inline": True},
        {"name": "🚨 Net Gain",       "value": f"**`+${gain:,}`**\nThreshold: `>${FLAG_THRESHOLD:,}`",                          "inline": True},
        {"name": "🕐 First Detected", "value": detected_str,                                                                     "inline": True},
        {"name": "🎮 Session Start",  "value": session_str,                                                                      "inline": True},
        {"name": "🔁 Checks",         "value": f"{update_count + 1}×",                                                           "inline": True},
    ]

    if change_log:
        fields.append({"name": "📋 Change Log", "value": "\n".join(change_log[-5:]), "inline": False})

    fields.append({
        "name":  "🔎 Review",
        "value": "[**Open Economy Flags →**](https://web.cfrp.co.za/analytics/economy/flags/page)",
        "inline": False,
    })

    return {
        "title":       title,
        "description": description,
        "color":       colour,
        "fields":      fields,
        "footer":      {"text": f"CFRP Economy Monitor  •  Update #{update_count + 1}" if is_update else "CFRP Economy Monitor  •  New Flag"},
        "timestamp":   now.isoformat(),
    }


def _post_webhook(embed) -> str | None:
    url = _webhook_url()
    if not url:
        return None
    try:
        import requests as req
        r = req.post(url + "?wait=true", json={"embeds": [embed]}, timeout=8)
        if r.ok:
            return str(r.json().get("id", ""))
    except Exception:
        pass
    return None


def _patch_webhook(message_id: str, embed):
    url = _webhook_url()
    if not url or not message_id:
        return
    try:
        import requests as req
        req.patch(url + f"/messages/{message_id}", json={"embeds": [embed]}, timeout=8)
    except Exception:
        pass


# ── Core check (called from background thread) ────────────────────────────────

def _check_live_players(app):
    from app.models import EconomyFlag
    import json as _json

    # Each call gets a brand-new app context → fresh SQLAlchemy scoped session.
    # This is critical: if we reuse a long-lived context the session cache will
    # return stale query results (e.g. reviewed flags not visible) causing
    # duplicate flag creation every tick.
    with app.app_context():
        try:
            db.session.remove()   # discard any leftover session state from previous tick
            now        = datetime.now(timezone.utc)
            window_ago = now - timedelta(hours=24)
            cutoff_90s = now - timedelta(seconds=90)

            live_hbs = PlayerHeartbeat.query.filter(
                PlayerHeartbeat.last_seen >= cutoff_90s
            ).all()

            if not live_hbs:
                return

            for hb in live_hbs:
                discord_id  = hb.discord_id
                player_name = hb.player_name or "Unknown"
                cash_now    = hb.cash or 0
                bank_now    = hb.bank or 0

                session_before = PlayerSession.query.filter(
                    PlayerSession.discord_id == discord_id,
                    PlayerSession.leave_time < window_ago,
                    PlayerSession.cash_at_leave.isnot(None),
                ).order_by(PlayerSession.leave_time.desc()).first()

                cash_before   = session_before.cash_at_leave or 0 if session_before else 0
                bank_before   = session_before.bank_at_leave or 0 if session_before else 0
                gain          = (cash_now + bank_now) - (cash_before + bank_before)

                if gain < FLAG_THRESHOLD:
                    continue

                # Session start time
                session_start_time = None
                if hb.session_id:
                    s = PlayerSession.query.get(hb.session_id)
                    if s and s.join_time:
                        session_start_time = s.join_time.replace(tzinfo=timezone.utc)

                # ── Max reviewed gain for this player in the 24h window ──────
                # Use MAX across ALL reviewed flags so we always compare against
                # the highest already-seen amount, not just the most recent one.
                from sqlalchemy import func as _func
                max_reviewed = db.session.query(
                    _func.max(EconomyFlag.amount_gain)
                ).filter(
                    EconomyFlag.discord_id  == discord_id,
                    EconomyFlag.is_reviewed == True,
                    EconomyFlag.detected_at >= window_ago,
                ).scalar() or 0

                # If there are reviewed flags and gain hasn't grown by a full
                # extra ALERT_STEP beyond the highest reviewed amount → stay
                # completely silent: no new flags, no webhook updates.
                if max_reviewed > 0 and gain < max_reviewed + ALERT_STEP:
                    continue

                # ── Look for an active (unreviewed) flag ─────────────────────
                existing = EconomyFlag.query.filter(
                    EconomyFlag.discord_id  == discord_id,
                    EconomyFlag.is_reviewed == False,
                    EconomyFlag.detected_at >= window_ago,
                ).order_by(EconomyFlag.detected_at.desc()).first()

                if existing:
                    prev_gain = existing.amount_gain or 0
                    # Only update + re-post if gain changed by >= $100
                    if abs(gain - prev_gain) < 100:
                        continue

                    # Change log stored as JSON in a separate field
                    try:
                        change_log = _json.loads(existing.flag_type.split("|||", 1)[1]) if "|||" in (existing.flag_type or "") else []
                    except Exception:
                        change_log = []

                    ts   = now.strftime("%H:%M:%S")
                    diff = gain - prev_gain
                    sign = "+" if diff >= 0 else ""
                    change_log.append(f"`{ts}` — `${gain:,}` ({sign}${diff:,})")
                    existing.flag_type   = f"large_gain|||{_json.dumps(change_log)}"
                    existing.amount_gain = gain
                    existing.cash_after  = cash_now
                    existing.bank_after  = bank_now
                    existing.window_end  = now
                    db.session.commit()

                    detected_at = existing.detected_at.replace(tzinfo=timezone.utc) if existing.detected_at else now
                    embed = _build_embed(
                        player_name, discord_id, gain,
                        cash_before, bank_before, cash_now, bank_now,
                        detected_at=detected_at,
                        session_start=session_start_time,
                        prev_gain=prev_gain,
                        update_count=len(change_log),
                        change_log=change_log,
                    )
                    if existing.discord_message_id:
                        _patch_webhook(existing.discord_message_id, embed)

                else:
                    # No unreviewed flag and gain cleared the next milestone
                    # (or there are no reviewed flags at all) → fresh alert.
                    change_log = [f"`{now.strftime('%H:%M:%S')}` — First detected `${gain:,}`"]
                    embed = _build_embed(
                        player_name, discord_id, gain,
                        cash_before, bank_before, cash_now, bank_now,
                        detected_at=now,
                        session_start=session_start_time,
                        prev_gain=None,
                        update_count=0,
                        change_log=change_log,
                    )
                    _dedup = f"{discord_id}:{gain}:{now.strftime('%Y%m%d%H%M')}"
                    # Skip silently if this exact dedup key already exists (race guard)
                    if EconomyFlag.query.filter_by(dedup_key=_dedup).first():
                        continue
                    flag = EconomyFlag(
                        discord_id   = discord_id,
                        player_name  = player_name,
                        flag_type    = f"large_gain|||{_json.dumps(change_log)}",
                        amount_gain  = gain,
                        cash_before  = cash_before,
                        bank_before  = bank_before,
                        cash_after   = cash_now,
                        bank_after   = bank_now,
                        window_start = window_ago,
                        window_end   = now,
                        dedup_key    = _dedup,
                    )
                    db.session.add(flag)
                    try:
                        db.session.flush()
                    except Exception:
                        # Unique constraint violation = duplicate, skip silently
                        db.session.rollback()
                        continue
                    msg_id = _post_webhook(embed)
                    if msg_id:
                        flag.discord_message_id = msg_id
                    db.session.commit()   # commit immediately — prevents duplicate creation on next tick

        except Exception as e:
            db.session.rollback()
            import logging
            logging.getLogger("cfrp.economy_monitor").exception("Live flag check failed: %s", e)
        finally:
            db.session.remove()   # always release session back to pool after each tick


def start_economy_monitor(app):
    """Spawn a daemon thread that checks every 20 seconds."""
    global _monitor_started
    if _monitor_started:
        return
    _monitor_started = True

    import threading, time

    def _loop():
        time.sleep(5)   # short initial delay so the app finishes starting
        while True:
            _check_live_players(app)
            time.sleep(20)

    t = threading.Thread(target=_loop, name="economy-monitor", daemon=True)
    t.start()


# ── Manual scan endpoint (kept for on-demand use) ────────────────────────────

def _run_economy_flag_check():
    """
    Manual/scheduled scan: also checks players who recently logged off
    (not just currently online). Used by the 'Run Scan Now' button.
    """
    from app.models import EconomyFlag
    now        = datetime.now(timezone.utc)
    window_ago = now - timedelta(hours=24)
    cutoff_90s = now - timedelta(seconds=90)

    # Live players + recently offline (sessions ended in last 48h)
    live_ids = {
        hb.discord_id
        for hb in PlayerHeartbeat.query.filter(
            PlayerHeartbeat.last_seen >= cutoff_90s
        ).all()
    }
    offline_ids = {
        r.discord_id for r in
        PlayerSession.query.filter(
            PlayerSession.leave_time >= now - timedelta(hours=48),
            PlayerSession.cash_at_leave.isnot(None),
        ).with_entities(PlayerSession.discord_id).all()
    }
    all_ids = live_ids | offline_ids
    flags_created = 0

    for discord_id in all_ids:
        # Skip if there's already an unreviewed flag — live monitor handles updates
        existing = EconomyFlag.query.filter(
            EconomyFlag.discord_id == discord_id,
            EconomyFlag.is_reviewed == False,
            EconomyFlag.detected_at >= window_ago,
        ).first()
        if existing:
            continue

        # Baseline
        session_before = PlayerSession.query.filter(
            PlayerSession.discord_id == discord_id,
            PlayerSession.leave_time < window_ago,
            PlayerSession.cash_at_leave.isnot(None),
        ).order_by(PlayerSession.leave_time.desc()).first()
        cash_before  = session_before.cash_at_leave or 0 if session_before else 0
        bank_before  = session_before.bank_at_leave or 0 if session_before else 0

        # Current wealth
        if discord_id in live_ids:
            hb = PlayerHeartbeat.query.filter_by(discord_id=discord_id).first()
            cash_now = hb.cash or 0 if hb else 0
            bank_now = hb.bank or 0 if hb else 0
            pname    = hb.player_name if hb else 'Unknown'
        else:
            latest = PlayerSession.query.filter(
                PlayerSession.discord_id == discord_id,
                PlayerSession.leave_time >= window_ago,
                PlayerSession.cash_at_leave.isnot(None),
            ).order_by(PlayerSession.leave_time.desc()).first()
            if not latest:
                continue
            cash_now = latest.cash_at_leave  or 0
            bank_now = latest.bank_at_leave  or 0
            pname    = latest.player_name or 'Unknown'

        gain = (cash_now + bank_now) - (cash_before + bank_before)
        if gain < FLAG_THRESHOLD:
            continue

        # Use MAX reviewed gain so multiple reviewed flags don't confuse
        # the threshold (same fix as the live monitor).
        from sqlalchemy import func as _func
        max_reviewed = db.session.query(
            _func.max(EconomyFlag.amount_gain)
        ).filter(
            EconomyFlag.discord_id  == discord_id,
            EconomyFlag.is_reviewed == True,
            EconomyFlag.detected_at >= window_ago,
        ).scalar() or 0

        if max_reviewed > 0 and gain < max_reviewed + ALERT_STEP:
            continue  # still within reviewed band — stay quiet

        _dedup = f"{discord_id}:{gain}:{now.strftime('%Y%m%d%H%M')}"
        if EconomyFlag.query.filter_by(dedup_key=_dedup).first():
            continue
        flag = EconomyFlag(
            discord_id   = discord_id,
            player_name  = pname,
            flag_type    = 'large_gain',
            amount_gain  = gain,
            cash_before  = cash_before,
            bank_before  = bank_before,
            cash_after   = cash_now,
            bank_after   = bank_now,
            window_start = window_ago,
            window_end   = now,
            dedup_key    = _dedup,
        )
        db.session.add(flag)
        try:
            db.session.flush()
        except Exception:
            db.session.rollback()
            continue
        embed  = _build_embed(pname, discord_id, gain, cash_before, bank_before, cash_now, bank_now)
        msg_id = _post_webhook(embed)
        if msg_id:
            flag.discord_message_id = msg_id
        flags_created += 1

    if flags_created:
        db.session.commit()
    return flags_created


@analytics_bp.route('/economy/check-flags', methods=['POST'])
@login_required
@_admin_required
def run_flag_check():
    """Manually trigger the flag detection scan."""
    from app.models import EconomyFlag
    try:
        count = _run_economy_flag_check()
        total = EconomyFlag.query.filter_by(is_reviewed=False).count()
        return jsonify({'flags_created': count, 'total_unreviewed': total, 'success': True})
    except Exception as e:
        return jsonify({'flags_created': 0, 'success': False, 'error': str(e)}), 500


@analytics_bp.route('/economy/flags')
@login_required
@_admin_required
def economy_flags():
    """List all economy flags (unreviewed first)."""
    from app.models import EconomyFlag
    flags = EconomyFlag.query.order_by(
        EconomyFlag.is_reviewed.asc(),
        EconomyFlag.detected_at.desc()
    ).limit(100).all()
    unreviewed = EconomyFlag.query.filter_by(is_reviewed=False).count()
    return jsonify({
        'flags': [f.to_dict() for f in flags],
        'unreviewed_count': unreviewed,
    })


@analytics_bp.route('/economy/flags/<int:flag_id>/review', methods=['POST'])
@login_required
@_admin_required
def review_flag(flag_id):
    """Mark a flag as reviewed with optional note."""
    from app.models import EconomyFlag
    flag = EconomyFlag.query.get_or_404(flag_id)
    data = request.get_json(silent=True) or {}
    flag.is_reviewed  = True
    flag.review_note  = data.get('note', '')
    flag.reviewed_by  = current_user.id
    flag.reviewed_at  = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'success': True})


@analytics_bp.route('/economy/flags/<int:flag_id>/delete', methods=['POST'])
@login_required
@_admin_required
def delete_flag(flag_id):
    """Hard-delete a single economy flag."""
    from app.models import EconomyFlag
    flag = EconomyFlag.query.get_or_404(flag_id)
    db.session.delete(flag)
    AuditLog.log('economy_flag.delete', user_id=current_user.id,
                 resource_type='economy_flag', resource_id=flag_id,
                 details={'player': flag.player_name, 'discord_id': flag.discord_id})
    db.session.commit()
    return jsonify({'success': True})


@analytics_bp.route('/economy/flags/clear', methods=['POST'])
@login_required
@_admin_required
def clear_flags():
    """
    Bulk-delete economy flags.
    Body: { "mode": "reviewed" | "player" | "all", "discord_id": "..." }
    """
    from app.models import EconomyFlag
    data    = request.get_json(silent=True) or {}
    mode    = data.get('mode', 'reviewed')
    disc_id = data.get('discord_id', '').strip()

    q = EconomyFlag.query
    if mode == 'reviewed':
        q = q.filter_by(is_reviewed=True)
    elif mode == 'player' and disc_id:
        q = q.filter_by(discord_id=disc_id)
    elif mode == 'all':
        pass  # delete everything
    else:
        return jsonify({'success': False, 'error': 'Invalid mode or missing discord_id'}), 400

    count = q.count()
    q.delete(synchronize_session=False)
    AuditLog.log('economy_flag.bulk_clear', user_id=current_user.id,
                 resource_type='economy_flag',
                 details={'mode': mode, 'discord_id': disc_id or None, 'deleted': count})
    db.session.commit()
    return jsonify({'success': True, 'deleted': count})


@analytics_bp.route('/economy/player/<discord_id>/history')
@login_required
@_admin_required
def player_economy_history(discord_id):
    """
    Day-by-day economy history for a player over the last 30 days.
    Uses session cash_at_leave / bank_at_leave as end-of-day snapshots.
    """
    from app.models import EconomyFlag
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=30)

    sessions = PlayerSession.query.filter(
        PlayerSession.discord_id == discord_id,
        PlayerSession.leave_time >= start,
        PlayerSession.cash_at_leave.isnot(None),
    ).order_by(PlayerSession.leave_time).all()

    # Build day-by-day: last session each day = end-of-day snapshot
    days = {}
    for s in sessions:
        day_key = s.leave_time.strftime('%Y-%m-%d')
        days[day_key] = {
            'date':          day_key,
            'cash':          s.cash_at_leave  or 0,
            'bank':          s.bank_at_leave  or 0,
            'total':         (s.cash_at_leave or 0) + (s.bank_at_leave or 0),
            'sessions_that_day': 0,
            'playtime_seconds':  0,
        }

    # Count sessions and playtime per day
    for s in sessions:
        day_key = s.leave_time.strftime('%Y-%m-%d')
        if day_key in days:
            days[day_key]['sessions_that_day'] += 1
            days[day_key]['playtime_seconds']  += (s.duration_seconds or 0)

    # Compute daily gain vs previous day
    sorted_days = sorted(days.values(), key=lambda d: d['date'])
    for i, day in enumerate(sorted_days):
        if i == 0:
            day['gain'] = 0
        else:
            day['gain'] = day['total'] - sorted_days[i-1]['total']

    # Flags for this player
    flags = EconomyFlag.query.filter_by(discord_id=discord_id)\
        .order_by(EconomyFlag.detected_at.desc()).limit(20).all()

    # Player name
    latest_session = PlayerSession.query.filter_by(discord_id=discord_id)\
        .order_by(PlayerSession.leave_time.desc()).first()
    player_name = (latest_session.player_name if latest_session else None) or 'Unknown'

    return jsonify({
        'discord_id':  discord_id,
        'player_name': player_name,
        'history':     sorted_days,
        'flags':       [f.to_dict() for f in flags],
        'total_sessions_30d': len(sessions),
        'peak_wealth': max((d['total'] for d in sorted_days), default=0),
        'current_cash': sorted_days[-1]['cash'] if sorted_days else 0,
        'current_bank': sorted_days[-1]['bank'] if sorted_days else 0,
    })


@analytics_bp.route('/economy/player/<discord_id>/statement')
@login_required
@_admin_required
def player_economy_statement_pdf(discord_id):
    from app.models import EconomyFlag
    from flask import make_response

    now      = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=30)

    sessions = PlayerSession.query.filter(
        PlayerSession.discord_id == discord_id,
        PlayerSession.leave_time >= start_dt,
        PlayerSession.cash_at_leave.isnot(None),
    ).order_by(PlayerSession.leave_time).all()

    latest      = PlayerSession.query.filter_by(discord_id=discord_id)\
        .order_by(PlayerSession.leave_time.desc()).first()
    player_name = (latest.player_name if latest else None) or 'Unknown'

    days = {}
    for s in sessions:
        dk = s.leave_time.strftime('%Y-%m-%d')
        days[dk] = {'date': dk, 'cash': s.cash_at_leave or 0,
                    'bank': s.bank_at_leave or 0,
                    'total': (s.cash_at_leave or 0) + (s.bank_at_leave or 0),
                    'sessions': 0}
    for s in sessions:
        dk = s.leave_time.strftime('%Y-%m-%d')
        if dk in days:
            days[dk]['sessions'] += 1

    sorted_days = sorted(days.values(), key=lambda d: d['date'])
    for i, day in enumerate(sorted_days):
        day['gain'] = day['total'] - sorted_days[i-1]['total'] if i > 0 else 0

    flags        = EconomyFlag.query.filter_by(discord_id=discord_id)\
        .order_by(EconomyFlag.detected_at.desc()).all()
    peak         = max((d['total'] for d in sorted_days), default=0)
    latest_total = sorted_days[-1]['total'] if sorted_days else 0
    total_gain   = sum(d['gain'] for d in sorted_days if d['gain'] > 0)

    def fmt(n):
        return f'${n:,}'

    rows_html = ''
    for day in reversed(sorted_days):
        gain_color = '#10b981' if day['gain'] >= 0 else '#f87171'
        flag_row   = abs(day['gain']) >= 10000
        row_bg     = 'background:#2d1515;' if flag_row else ''
        gain_str   = ('+' if day['gain'] >= 0 else '') + fmt(day['gain'])
        flag_icon  = ' ⚠️' if flag_row else ''
        rows_html += f"""<tr style="{row_bg}">
          <td style="color:#e5e7eb;font-family:monospace;">{day['date']}{flag_icon}</td>
          <td style="color:#fbbf24;">{fmt(day['cash'])}</td>
          <td style="color:#60a5fa;">{fmt(day['bank'])}</td>
          <td style="color:#f9fafb;font-weight:700;">{fmt(day['total'])}</td>
          <td style="color:{gain_color};font-weight:600;">{gain_str}</td>
          <td style="text-align:center;color:#9ca3af;">{day['sessions']}</td>
        </tr>"""

    flags_html = ''
    for f in flags:
        det    = f.detected_at.strftime('%d %b %Y %H:%M') if f.detected_at else '—'
        before = fmt((f.cash_before or 0) + (f.bank_before or 0))
        after  = fmt((f.cash_after  or 0) + (f.bank_after  or 0))
        rev    = '<span style="color:#10b981;">Yes</span>' if f.is_reviewed else '<span style="color:#f87171;">No</span>'
        note   = f.review_note or '—'
        flags_html += f"""<tr>
          <td style="color:#9ca3af;font-family:monospace;font-size:8pt;">{det}</td>
          <td style="color:#f87171;font-weight:700;">{fmt(f.amount_gain or 0)}</td>
          <td style="color:#e5e7eb;">{before}</td>
          <td style="color:#e5e7eb;">{after}</td>
          <td>{rev}</td>
          <td style="color:#9ca3af;">{note}</td>
        </tr>"""
    if not flags_html:
        flags_html = '<tr><td colspan="6" style="text-align:center;color:#6b7280;padding:1.5rem;">No flags on this account</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Economy Statement — {player_name}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}

  body {{
    font-family: 'Inter', 'Segoe UI', Arial, sans-serif;
    font-size: 10pt;
    color: #e5e7eb;
    background: #111827;
    padding: 0;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }}

  /* ── Print toolbar ──────────────────────────────────────────────────── */
  .toolbar {{
    background: #1f2937;
    border-bottom: 1px solid #374151;
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }}
  .toolbar-hint {{
    font-size: 9pt;
    color: #9ca3af;
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .print-btn {{
    background: #4f46e5;
    color: #fff;
    border: none;
    border-radius: 8px;
    padding: 9px 20px;
    font-size: 10pt;
    font-weight: 600;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
    transition: background 0.15s;
  }}
  .print-btn:hover {{ background: #4338ca; }}

  /* ── Page wrapper ───────────────────────────────────────────────────── */
  .page {{
    max-width: 960px;
    margin: 0 auto;
    padding: 2rem 2.5rem 3rem;
  }}

  /* ── Header ─────────────────────────────────────────────────────────── */
  .header {{
    border-bottom: 1px solid #374151;
    padding-bottom: 1.25rem;
    margin-bottom: 1.5rem;
  }}
  .header-top {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    margin-bottom: 0.75rem;
  }}
  .logo-pill {{
    background: #4f46e5;
    color: #fff;
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    border-radius: 999px;
    padding: 4px 12px;
  }}
  .generated-at {{
    font-size: 8pt;
    color: #6b7280;
    text-align: right;
  }}
  h1 {{
    font-size: 20pt;
    font-weight: 700;
    color: #f9fafb;
    margin-bottom: 4px;
  }}
  .player-meta {{
    display: flex;
    gap: 1.5rem;
    font-size: 9pt;
    color: #9ca3af;
    margin-top: 6px;
  }}
  .player-meta strong {{ color: #e5e7eb; }}

  /* ── Stat cards ─────────────────────────────────────────────────────── */
  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 12px;
    margin-bottom: 2rem;
  }}
  .stat-card {{
    background: #1f2937;
    border: 1px solid #374151;
    border-radius: 10px;
    padding: 14px 16px;
  }}
  .stat-val {{
    font-size: 14pt;
    font-weight: 700;
    font-family: 'Courier New', monospace;
    margin-bottom: 4px;
  }}
  .stat-lbl {{
    font-size: 7.5pt;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.07em;
  }}

  /* ── Section headings ───────────────────────────────────────────────── */
  .section-heading {{
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 11pt;
    font-weight: 700;
    color: #f9fafb;
    margin: 1.75rem 0 0.875rem;
  }}
  .section-heading .pill {{
    font-size: 7.5pt;
    font-weight: 600;
    background: #374151;
    color: #9ca3af;
    border-radius: 999px;
    padding: 2px 10px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }}

  /* ── Tables ─────────────────────────────────────────────────────────── */
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 9pt;
    background: #1f2937;
    border-radius: 10px;
    overflow: hidden;
  }}
  thead tr {{
    background: #374151;
  }}
  th {{
    padding: 9px 12px;
    text-align: left;
    font-size: 8pt;
    font-weight: 600;
    color: #9ca3af;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    border-bottom: 1px solid #4b5563;
  }}
  td {{
    padding: 8px 12px;
    border-bottom: 1px solid #374151;
    color: #d1d5db;
  }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover td {{ background: #263244; }}

  .flag-thead th {{
    background: #7f1d1d;
    color: #fca5a5;
    border-bottom: 1px solid #991b1b;
  }}

  /* ── Footer ─────────────────────────────────────────────────────────── */
  .footer {{
    margin-top: 2.5rem;
    padding-top: 1rem;
    border-top: 1px solid #374151;
    font-size: 7.5pt;
    color: #4b5563;
    display: flex;
    justify-content: space-between;
  }}

  /* ── Print overrides ────────────────────────────────────────────────── */
  @media print {{
    .toolbar {{ display: none !important; }}
    body {{ background: #111827; padding: 0; }}
    .page {{ padding: 1.5cm; max-width: 100%; }}
    @page {{ margin: 1cm; size: A4; background: #111827; }}
    * {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
  }}
</style>
</head>
<body>

<!-- Toolbar (hidden on print) -->
<div class="toolbar">
  <div class="toolbar-hint">
    💡 Press <kbd style="background:#374151;border:1px solid #4b5563;border-radius:4px;padding:2px 6px;font-family:monospace;">Ctrl+P</kbd>
    and select <strong style="color:#e5e7eb;">"Save as PDF"</strong> — enable <em>Background Graphics</em> for the dark theme.
  </div>
  <button class="print-btn" onclick="window.print()">
    🖨&nbsp; Print / Save PDF
  </button>
</div>

<div class="page">

  <!-- Header -->
  <div class="header">
    <div class="header-top">
      <span class="logo-pill">CFRP</span>
      <div class="generated-at">
        Generated {now.strftime('%d %b %Y %H:%M')} UTC<br>
        <span style="color:#4b5563;">Confidential</span>
      </div>
    </div>
    <h1>Economy Statement</h1>
    <div class="player-meta">
      <span>Player: <strong>{player_name}</strong></span>
      <span>Discord: <strong style="font-family:monospace;">{discord_id}</strong></span>
      <span>Period: <strong>{start_dt.strftime('%d %b %Y')} – {now.strftime('%d %b %Y')}</strong></span>
    </div>
  </div>

  <!-- Summary stats -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-val" style="color:#818cf8;">{fmt(latest_total)}</div>
      <div class="stat-lbl">Current Wealth</div>
    </div>
    <div class="stat-card">
      <div class="stat-val" style="color:#fbbf24;">{fmt(peak)}</div>
      <div class="stat-lbl">Peak (30 days)</div>
    </div>
    <div class="stat-card">
      <div class="stat-val" style="color:#10b981;">{fmt(total_gain)}</div>
      <div class="stat-lbl">Total Earned</div>
    </div>
    <div class="stat-card">
      <div class="stat-val" style="color:#e5e7eb;">{len(sessions)}</div>
      <div class="stat-lbl">Sessions</div>
    </div>
    <div class="stat-card">
      <div class="stat-val" style="color:{'#f87171' if flags else '#10b981'};">{len(flags)}</div>
      <div class="stat-lbl">Flags Raised</div>
    </div>
  </div>

  <!-- History table -->
  <div class="section-heading">
    Day-by-Day Wealth History
    <span class="pill">{len(sorted_days)} days</span>
  </div>
  <table>
    <thead>
      <tr>
        <th>Date</th><th>Cash</th><th>Bank</th>
        <th>Total Wealth</th><th>Daily Change</th><th>Sessions</th>
      </tr>
    </thead>
    <tbody>
      {rows_html if rows_html else f'<tr><td colspan="6" style="text-align:center;color:#6b7280;padding:1.5rem;">No session data in this period</td></tr>'}
    </tbody>
  </table>

  <!-- Flags table -->
  <div class="section-heading">
    Economy Flags
    <span class="pill">{'none' if not flags else str(len(flags))}</span>
  </div>
  <table>
    <thead class="flag-thead">
      <tr>
        <th>Detected</th><th>Gain</th><th>Before</th>
        <th>After</th><th>Reviewed</th><th>Note</th>
      </tr>
    </thead>
    <tbody>{flags_html}</tbody>
  </table>

  <!-- Footer -->
  <div class="footer">
    <span>CFRP Economy Statement · Confidential</span>
    <span>Generated {now.strftime('%d %b %Y %H:%M')} UTC</span>
  </div>

</div>
</body>
</html>"""

    response = make_response(html)
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response

    """
    Return a print-ready HTML statement for a player.
    The browser handles print-to-PDF via Ctrl+P / browser print dialog.
    No external PDF library required.
    """
    from app.models import EconomyFlag
    from flask import make_response

    now   = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=30)

    sessions = PlayerSession.query.filter(
        PlayerSession.discord_id == discord_id,
        PlayerSession.leave_time >= start_dt,
        PlayerSession.cash_at_leave.isnot(None),
    ).order_by(PlayerSession.leave_time).all()

    latest = PlayerSession.query.filter_by(discord_id=discord_id)\
        .order_by(PlayerSession.leave_time.desc()).first()
    player_name = (latest.player_name if latest else None) or 'Unknown'

    days = {}
    for s in sessions:
        dk = s.leave_time.strftime('%Y-%m-%d')
        days[dk] = {'date': dk, 'cash': s.cash_at_leave or 0,
                    'bank': s.bank_at_leave or 0,
                    'total': (s.cash_at_leave or 0) + (s.bank_at_leave or 0),
                    'sessions': 0}
    for s in sessions:
        dk = s.leave_time.strftime('%Y-%m-%d')
        if dk in days:
            days[dk]['sessions'] += 1

    sorted_days = sorted(days.values(), key=lambda d: d['date'])
    for i, day in enumerate(sorted_days):
        day['gain'] = day['total'] - sorted_days[i-1]['total'] if i > 0 else 0

    flags = EconomyFlag.query.filter_by(discord_id=discord_id)\
        .order_by(EconomyFlag.detected_at.desc()).all()

    peak         = max((d['total'] for d in sorted_days), default=0)
    latest_total = sorted_days[-1]['total'] if sorted_days else 0
    total_gain   = sum(d['gain'] for d in sorted_days if d['gain'] > 0)

    def fmt(n):
        return f'${n:,}'

    # Build the HTML rows
    rows_html = ''
    for day in reversed(sorted_days):
        gain_color = '#16a34a' if day['gain'] >= 0 else '#dc2626'
        flag_row = abs(day['gain']) >= 10000
        bg = 'background:#fef2f2;' if flag_row else ''
        gain_str = ('+' if day['gain'] >= 0 else '') + fmt(day['gain'])
        flag_icon = ' ⚠' if flag_row else ''
        rows_html += f"""<tr style="{bg}">
          <td>{day['date']}{flag_icon}</td>
          <td style="color:#d97706;">{fmt(day['cash'])}</td>
          <td style="color:#2563eb;">{fmt(day['bank'])}</td>
          <td style="font-weight:700;">{fmt(day['total'])}</td>
          <td style="color:{gain_color};font-weight:600;">{gain_str}</td>
          <td style="text-align:center;">{day['sessions']}</td>
        </tr>"""

    flags_html = ''
    for f in flags:
        det = f.detected_at.strftime('%d %b %Y %H:%M') if f.detected_at else '—'
        before = fmt((f.cash_before or 0) + (f.bank_before or 0))
        after  = fmt((f.cash_after  or 0) + (f.bank_after  or 0))
        rev    = 'Yes' if f.is_reviewed else '<span style="color:#dc2626;">No</span>'
        note   = f.review_note or '—'
        flags_html += f"""<tr>
          <td>{det}</td>
          <td style="color:#dc2626;font-weight:700;">{fmt(f.amount_gain or 0)}</td>
          <td>{before}</td>
          <td>{after}</td>
          <td>{rev}</td>
          <td>{note}</td>
        </tr>"""
    if not flags_html:
        flags_html = '<tr><td colspan="6" style="text-align:center;color:#6b7280;">No flags on this account</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Economy Statement — {player_name}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; font-size: 11pt; color: #111; background: #fff; padding: 2cm; }}
  h1 {{ font-size: 20pt; color: #6366f1; margin-bottom: 4px; }}
  .sub {{ color: #6b7280; font-size: 9pt; margin-bottom: 2px; }}
  hr {{ border: none; border-top: 1px solid #e5e7eb; margin: 16px 0; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(3,1fr); gap: 12px; margin-bottom: 20px; }}
  .stat-box {{ background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px; }}
  .stat-box .val {{ font-size: 15pt; font-weight: 700; font-family: monospace; }}
  .stat-box .lbl {{ font-size: 8pt; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; margin-top: 3px; }}
  h2 {{ font-size: 12pt; color: #1f2937; margin: 18px 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 9.5pt; }}
  th {{ background: #6366f1; color: #fff; padding: 7px 8px; text-align: left; font-size: 8.5pt; }}
  td {{ padding: 6px 8px; border-bottom: 1px solid #f3f4f6; }}
  tr:nth-child(even) td {{ background: #f9fafb; }}
  .flag-th {{ background: #dc2626; }}
  .footer {{ margin-top: 24px; font-size: 8pt; color: #9ca3af; text-align: center; }}
  .no-print {{ margin-bottom: 20px; }}
  @media print {{
    .no-print {{ display: none !important; }}
    body {{ padding: 1.5cm; }}
    @page {{ margin: 1.5cm; }}
  }}
</style>
</head>
<body>
<div class="no-print" style="background:#6366f122;border:1px solid #6366f144;border-radius:8px;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;">
  <span style="font-size:10pt;color:#4338ca;font-weight:600;"><i>💡 Use your browser's Print (Ctrl+P) and select "Save as PDF" to download.</i></span>
  <button onclick="window.print()" style="background:#6366f1;color:#fff;border:none;border-radius:6px;padding:8px 16px;font-size:10pt;cursor:pointer;font-weight:600;">🖨 Print / Save PDF</button>
</div>

<h1>CFRP Economy Statement</h1>
<div class="sub">Player: <strong>{player_name}</strong> &nbsp;|&nbsp; Discord: {discord_id}</div>
<div class="sub">Period: {start_dt.strftime('%d %b %Y')} – {now.strftime('%d %b %Y')} &nbsp;|&nbsp; Generated: {now.strftime('%d %b %Y %H:%M')} UTC</div>
<hr>

<div class="summary-grid">
  <div class="stat-box"><div class="val" style="color:#6366f1;">{fmt(latest_total)}</div><div class="lbl">Current Total Wealth</div></div>
  <div class="stat-box"><div class="val" style="color:#d97706;">{fmt(peak)}</div><div class="lbl">Peak Wealth (30 days)</div></div>
  <div class="stat-box"><div class="val" style="color:#16a34a;">{fmt(total_gain)}</div><div class="lbl">Total Earned (30 days)</div></div>
  <div class="stat-box"><div class="val">{len(sessions)}</div><div class="lbl">Sessions (30 days)</div></div>
  <div class="stat-box"><div class="val" style="color:#dc2626;">{len(flags)}</div><div class="lbl">Economy Flags</div></div>
</div>

<h2>Day-by-Day Wealth History</h2>
<table>
  <thead><tr><th>Date</th><th>Cash</th><th>Bank</th><th>Total Wealth</th><th>Daily Change</th><th>Sessions</th></tr></thead>
  <tbody>{rows_html if rows_html else '<tr><td colspan="6" style="text-align:center;color:#6b7280;">No session data in this period</td></tr>'}</tbody>
</table>

<h2>Economy Flags ({len(flags)})</h2>
<table>
  <thead><tr class="flag-th"><th>Detected</th><th>Gain</th><th>Before</th><th>After</th><th>Reviewed</th><th>Note</th></tr></thead>
  <tbody>{flags_html}</tbody>
</table>

<div class="footer">CFRP Economy Statement &nbsp;·&nbsp; Confidential &nbsp;·&nbsp; Generated {now.strftime('%d %b %Y %H:%M')} UTC</div>
</body>
</html>"""

    response = make_response(html)
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response

@analytics_bp.route('/economy/flags/page')
@login_required
@_admin_required
def economy_flags_page():
    return render_template('analytics/economy_flags.html')


# ═══════════════════════════════════════════════════════════════════════════════
# ── Self-Healing AI Engine ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

import os as _os
import re as _re
import json as _json_sh
import textwrap as _textwrap
import threading as _threading

_SH_LOCK = _threading.Lock()   # prevent concurrent patch writes

# ── Source files the engine is allowed to read & patch ───────────────────────
_APP_ROOT = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), '..'))

_SOURCE_FILES = {
    'analytics/routes.py':    _os.path.join(_APP_ROOT, 'analytics', 'routes.py'),
    'models.py':              _os.path.join(_APP_ROOT, 'models.py'),
    'admin/routes.py':        _os.path.join(_APP_ROOT, 'admin', 'routes.py'),
    'api/routes.py':          _os.path.join(_APP_ROOT, 'api', 'routes.py'),
    'utils.py':               _os.path.join(_APP_ROOT, 'utils.py'),
    'config.py':              _os.path.join(_APP_ROOT, 'config.py'),
    '__init__.py':            _os.path.join(_APP_ROOT, '__init__.py'),
}

_SYSTEM_CONTEXT = """
You are the self-healing AI engine for the CFRP Whitelist application — a Flask/SQLAlchemy
FiveM community management system. You have deep knowledge of its architecture:

ROLES & PERMISSIONS (from models.py):
- admin   (priority 100): all permissions including admin.access, admin.users, admin.roles,
                          analytics.view, api.admin, etc.
- staff   (priority  50): admin.access, applications.review, applications.approve,
                          analytics.view
- member  (priority  10): api.access
- guest   (priority   0): no permissions

KEY MODELS:
- User: has_permission(name), has_role(name), is_admin property, primary_role
- Role: priority field (higher = more authority), has_permission(name)
- EconomyFlag: discord_id, amount_gain, is_reviewed, reviewed_by→User,
               detected_at, flag_type (stores JSON change_log after |||),
               discord_message_id (for webhook PATCH)
- PlayerHeartbeat: live player data, cash/bank, last_seen (stale after 90s)
- PlayerSession: cash_at_leave / bank_at_leave snapshots
- SiteSettings: key/value store with .get(key, default) / .set(key, value)
- AuditLog: .log(action, user_id, resource_type, resource_id, details, ip)

ECONOMY FLAG LOGIC RULES (hard-won, do not regress):
- FLAG_THRESHOLD = 50_000  (initial alert threshold)
- ALERT_STEP     = 50_000  (gain above MAX reviewed amount before re-alerting)
- Silencing rule: use MAX(amount_gain) across ALL reviewed flags in 24h window,
  NOT .order_by(detected_at.desc()).first() — that was the original bug.
- When a flag is reviewed, no new flag or webhook update until gain > max_reviewed + ALERT_STEP
- Live monitor (_check_live_players): patches existing unreviewed flag's Discord message
- Manual scan (_run_economy_flag_check): skips players with existing unreviewed flag

COMMON BUG PATTERNS TO RECOGNISE:
1. Using .first() when MAX() is needed for aggregated comparisons
2. Missing timezone.utc on datetime comparisons
3. Permission checks using is_admin when a specific permission should be checked
4. Missing db.session.rollback() in exception handlers
5. Flag deduplication using .first() on reviewed flags (always use MAX instead)
6. Race conditions in the economy monitor from missing the early-exit guard

When asked to analyse code: reason through the logic step by step, identify the root cause,
explain it clearly in plain English, then propose a minimal targeted fix.

When generating a patch, output ONLY a JSON object with this exact structure:
{
  "file": "relative/path.py",
  "find": "exact string to find (verbatim, including whitespace)",
  "replace": "replacement string",
  "explanation": "one sentence"
}
If no patch is needed, output: {"patch": null, "explanation": "reason"}
"""


def _read_source_snippet(file_key: str, around_line: int = None, lines: int = 60) -> str:
    path = _SOURCE_FILES.get(file_key)
    if not path or not _os.path.exists(path):
        return f"# [{file_key} not found]"
    with open(path, 'r') as fh:
        all_lines = fh.readlines()
    if around_line:
        start = max(0, around_line - lines // 2)
        end   = min(len(all_lines), around_line + lines // 2)
        return ''.join(all_lines[start:end])
    # Return full file but cap at 8000 chars
    full = ''.join(all_lines)
    return full[:8000] + ('\n# ... [truncated]' if len(full) > 8000 else '')


def _call_claude(messages: list, max_tokens: int = 2000) -> str:
    """Call the Anthropic API from the server side (uses ANTHROPIC_API_KEY env var)."""
    import requests as _req
    api_key = _os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return '{"error": "ANTHROPIC_API_KEY not set in environment"}'
    resp = _req.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        json={
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': max_tokens,
            'system': _SYSTEM_CONTEXT,
            'messages': messages,
        },
        timeout=60,
    )
    if not resp.ok:
        return f'{{"error": "API error {resp.status_code}: {resp.text[:200]}"}}'
    data = resp.json()
    return ''.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')


@analytics_bp.route('/self-heal')
@login_required
@_admin_required
def self_heal_ui():
    return render_template('analytics/self_heal.html')


@analytics_bp.route('/self-heal/analyse', methods=['POST'])
@login_required
@_admin_required
def self_heal_analyse():
    """
    Analyse a reported bug. The AI reads relevant source sections and
    returns a structured diagnosis + optional patch.
    """
    data       = request.get_json(silent=True) or {}
    bug_report = (data.get('report') or '').strip()
    file_hint  = data.get('file', 'analytics/routes.py')

    if not bug_report:
        return jsonify({'error': 'No bug report provided'}), 400

    # Build context: include the relevant source file + models
    source = _read_source_snippet(file_hint)
    models = _read_source_snippet('models.py')

    prompt = f"""Bug report from admin:
{bug_report}

---
Source file ({file_hint}):
```python
{source}
```

---
models.py (abridged):
```python
{models}
```

Analyse the bug. Think through:
1. What is the expected behaviour?
2. What is the actual behaviour and why?
3. Does this involve roles, permissions, or flag logic?
4. What is the minimal fix?

Then output your patch JSON."""

    reply = _call_claude([{'role': 'user', 'content': prompt}])

    # Try to extract JSON patch from reply
    patch = None
    explanation = reply
    try:
        # Find JSON block in reply
        m = _re.search(r'\{[\s\S]*"file"[\s\S]*\}', reply)
        if m:
            patch_obj = _json_sh.loads(m.group(0))
            patch = patch_obj
            explanation = patch_obj.get('explanation', reply)
        else:
            m2 = _re.search(r'\{[\s\S]*"patch"[\s\S]*null[\s\S]*\}', reply)
            if m2:
                obj = _json_sh.loads(m2.group(0))
                explanation = obj.get('explanation', reply)
    except Exception:
        pass

    return jsonify({
        'raw':         reply,
        'patch':       patch,
        'explanation': explanation,
    })


@analytics_bp.route('/self-heal/apply', methods=['POST'])
@login_required
@_admin_required
def self_heal_apply():
    """Apply a patch proposed by the AI engine."""
    data     = request.get_json(silent=True) or {}
    file_key = data.get('file', '')
    find     = data.get('find', '')
    replace  = data.get('replace', '')

    if not file_key or not find:
        return jsonify({'success': False, 'error': 'Missing file or find string'}), 400

    path = _SOURCE_FILES.get(file_key)
    if not path or not _os.path.exists(path):
        return jsonify({'success': False, 'error': f'File not found: {file_key}'}), 404

    with _SH_LOCK:
        with open(path, 'r') as fh:
            original = fh.read()

        if find not in original:
            return jsonify({'success': False, 'error': 'Target string not found in file — patch may be stale'}), 409

        count = original.count(find)
        if count > 1:
            return jsonify({'success': False, 'error': f'Target string is ambiguous ({count} matches) — refine the patch'}), 409

        patched = original.replace(find, replace, 1)

        # Write backup
        backup_path = path + '.selfheal.bak'
        with open(backup_path, 'w') as fh:
            fh.write(original)

        with open(path, 'w') as fh:
            fh.write(patched)

    from app.models import AuditLog
    AuditLog.log(
        action='self_heal_patch',
        user_id=current_user.id,
        resource_type='source_file',
        resource_id=file_key,
        details={'find_preview': find[:120], 'applied_by': current_user.username},
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify({'success': True, 'backup': backup_path})


@analytics_bp.route('/self-heal/chat', methods=['POST'])
@login_required
@_admin_required
def self_heal_chat():
    """
    Free-form chat with the AI about the codebase.
    Maintains a conversation history sent from the client.
    """
    data     = request.get_json(silent=True) or {}
    messages = data.get('messages', [])
    file_key = data.get('file', 'analytics/routes.py')

    if not messages:
        return jsonify({'error': 'No messages'}), 400

    # Inject source context into the first user message
    if len(messages) == 1:
        source  = _read_source_snippet(file_key)
        models  = _read_source_snippet('models.py')
        init    = _read_source_snippet('__init__.py')
        prefix  = (
            f"For context, here are the relevant source files:\n\n"
            f"**{file_key}** (excerpt):\n```python\n{source}\n```\n\n"
            f"**models.py** (excerpt):\n```python\n{models}\n```\n\n"
            f"**__init__.py** (excerpt):\n```python\n{init}\n```\n\n"
            f"---\n"
        )
        messages[0]['content'] = prefix + messages[0]['content']

    reply = _call_claude(messages, max_tokens=2500)
    return jsonify({'reply': reply})


@analytics_bp.route('/self-heal/files')
@login_required
@_admin_required
def self_heal_files():
    """Return list of readable source files with line counts."""
    result = []
    for key, path in _SOURCE_FILES.items():
        try:
            with open(path) as fh:
                lines = sum(1 for _ in fh)
            result.append({'key': key, 'lines': lines})
        except Exception:
            result.append({'key': key, 'lines': 0})
    return jsonify(result)


@analytics_bp.route('/self-heal/source')
@login_required
@_admin_required
def self_heal_source():
    """Return raw source of a file (for display in the UI)."""
    file_key = request.args.get('file', 'analytics/routes.py')
    path = _SOURCE_FILES.get(file_key)
    if not path or not _os.path.exists(path):
        return jsonify({'error': 'Not found'}), 404
    with open(path) as fh:
        content = fh.read()
    return jsonify({'content': content, 'lines': content.count('\n')})


# ═══════════════════════════════════════════════════════════════════════════════
# ── Auto-Diagnosis Engine ─────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

import json as _json_diag

# Reuse REASON_PATTERNS from above — these are already defined in data_crash_summary
# We define a standalone classify function here for the auto-diagnosis engine

_DIAG_PATTERNS = [
    ('Connection timed out', 'connection_timeout',
     'Connection Timeout',
     'The server stopped receiving heartbeats from the client. Usually caused by high ping, a blocking Lua thread, or a large reliable-channel event burst.',
     ['Check sv_timeout in server.cfg — raise to 60 if on default 30',
      'Look for blocking Wait(0) loops in Lua threads',
      'Check if a resource is sending large reliable-channel events',
      'Use netstat to verify server outbound bandwidth']),
    ('Timed out', 'connection_timeout',
     'Generic Timeout',
     'Client exceeded the server timeout window. Can be network-side or a server CPU spike.',
     ['Raise sv_timeout in server.cfg',
      'Check for CPU-intensive resources (Chromium NUI, heavy threads)',
      'Monitor txAdmin > Server > Resources for CPU % spikes']),
    ('Server->client flow control', 'flow_control',
     'Flow Control Kick',
     'Server sent data faster than the client could receive — usually a resource sending a burst of large events.',
     ['Search all resources for TriggerClientEvent inside for loops with no Wait()',
      'Compress large table payloads (use msgpack or split into batches)',
      'Check ox_inventory/qb-inventory item syncs']),
    ('client->server flow', 'flow_control',
     'Client→Server Flow Control',
     'Client was sending data faster than the server could process.',
     ['Look for TriggerServerEvent calls in resource Tick handlers',
      'Check NUI callbacks that fire on every frame',
      'Throttle client→server events with a cooldown']),
    ('Steam', 'steam_error',
     'Steam Auth Failure',
     'Steam ticket validation failed — player may have been offline or ticket expired.',
     ['Ensure steam_webApiKey is set in server.cfg',
      'Check https://store.steampowered.com/status for Steam outages',
      'Player losing internet briefly mid-session can cause this']),
    ('EAC', 'eac_kick',
     'EasyAntiCheat Kick',
     'EAC flagged the player or detected an integrity issue with game files.',
     ['Player may have modified game files — advise verifying GTA V integrity via Steam/Epic/R*',
      'Ensure EAC is enabled and up-to-date on server',
      'Persistent EAC kicks for same player = likely cheating tools']),
    ('Kicked', 'admin_kick',
     'Admin / Rule Kick',
     'Player was manually kicked by staff or an automated rule.',
     ['Review txAdmin action logs for the kick reason',
      'If automated (e.g. AFK kick), check the resource config for false positives']),
    ('model streaming', 'streaming_crash',
     'Model Streaming Failure',
     'Client failed to stream a model or asset causing a crash. Usually a bad prop or ydr/yft file.',
     ['Check for invalid prop hashes in resource stream folders',
      'Validate all .ydr/.yft/.ymap files with CodeWalker',
      'Look for models with missing collision files']),
    ('assert', 'script_assert',
     'Script Assertion / Lua Error',
     'A Lua assert() failed or a script threw an unhandled error at crash time.',
     ['Check F8 console logs on client — the assert message will name the resource',
      'Review recent resource updates — assertion errors often follow a bad update',
      'Enable set sv_scriptHookAllowed 0 if not already']),
    ('infinite loop', 'script_loop',
     'Infinite Loop / Hang',
     'A Lua script entered an infinite loop, freezing the game thread.',
     ['Search resources for while true do without Wait()',
      'Use citizen:setThreadIdentifier for long-running threads',
      'Check ESX/QB framework version for known loop bugs']),
    ('memory', 'oom',
     'Out of Memory / RAM',
     'Client ran out of memory — usually caused by streaming too many assets or texture leaks.',
     ['Reduce the number of simultaneously loaded DLC packs',
      'Check for texture memory leaks in custom MLOs',
      'Use an FPS limiter to reduce VRAM pressure']),
    ('native', 'native_crash',
     'Native / Engine Crash',
     'A game native function threw an exception — usually a null entity or invalid argument passed to a native.',
     ['Check any resource using GetEntityCoords or GetPedBone on null entities',
      'Wrap native calls with if DoesEntityExist(ent) then guards',
      'Use AddEventHandler("gameEventTriggered", ...) to catch entity-deleted events']),
    ('no active', 'no_license',
     'No Active License / Identifier',
     'Player connected without a valid Rockstar/Steam license.',
     ['Check identifiers in server.cfg — steam and license must be enabled',
      'Player may be using an unlicensed copy — reject with a clear message']),
    ('server shutting down', 'server_restart',
     'Server Restart',
     'Session ended because the server restarted or stopped.',
     ['Normal if planned — check txAdmin scheduled restarts',
      'If unplanned, check server logs for out-of-memory or crash exit']),
    ('quit', 'player_quit',
     'Player Quit',
     'Player chose to leave via the pause menu — a normal, expected disconnect.',
     ['Normal — no action needed']),
    ('Disconnected', 'client_disconnect',
     'Normal Disconnect',
     'Player disconnected normally (quit, kicked, or crash exit detected as clean).',
     ['No action needed unless count is unexpectedly high']),
]


def _auto_classify(reason: str, category: str):
    """Return (key, label, diagnosis_text, fix_steps) for a disconnect reason."""
    if not reason:
        return ('unclassified', 'Unclassified Disconnect',
                'No reason string was recorded for this disconnect.',
                ['Check your FiveM resource sends disconnect_reason on session end',
                 'Review txAdmin logs around this time for clues'])
    lower = reason.lower()
    for keyword, key, label, diag, steps in _DIAG_PATTERNS:
        if keyword.lower() in lower:
            return (key, label, diag, steps)
    return ('unclassified', 'Unclassified Disconnect',
            f'Reason string "{reason[:80]}" does not match any known FiveM error pattern.',
            ['Copy the reason string and search the cfx.re forum / FiveM Discord',
             'Check txAdmin logs for additional context around this time'])


def auto_diagnose_session(session):
    """
    Create a CrashDiagnosis record for a PlayerSession if it was a crash/timeout.
    Call this when a session ends (from your API route that records session end).
    Safe to call multiple times — will skip if a diagnosis already exists.
    """
    from app.models import CrashDiagnosis
    if session.disconnect_category not in ('crash', 'timeout', 'connection'):
        return None
    # Skip if already diagnosed
    existing = CrashDiagnosis.query.filter_by(session_id=session.id).first()
    if existing:
        return existing
    key, label, diag_text, fix_steps = _auto_classify(
        session.disconnect_reason or '',
        session.disconnect_category or ''
    )
    record = CrashDiagnosis(
        session_id      = session.id,
        discord_id      = session.discord_id,
        player_name     = session.player_name or 'Unknown',
        category        = session.disconnect_category,
        reason_raw      = session.disconnect_reason or '',
        diagnosis_key   = key,
        diagnosis_label = label,
        diagnosis_text  = diag_text,
        fix_steps       = _json_diag.dumps(fix_steps),
        ping_at_crash   = session.disconnect_ping,
        session_seconds = session.duration_seconds,
    )
    db.session.add(record)
    try:
        db.session.flush()
    except Exception:
        db.session.rollback()
        return None
    return record


def backfill_diagnoses():
    """
    Backfill diagnoses for all existing crash/timeout sessions that don't have one.
    Call once on startup or manually via the admin endpoint below.
    """
    from app.models import CrashDiagnosis
    sessions = PlayerSession.query.filter(
        PlayerSession.disconnect_category.in_(['crash', 'timeout', 'connection']),
    ).all()
    created = 0
    for s in sessions:
        existing = CrashDiagnosis.query.filter_by(session_id=s.id).first()
        if existing:
            continue
        r = auto_diagnose_session(s)
        if r:
            created += 1
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return created


@analytics_bp.route('/crashes/diagnose-all', methods=['POST'])
@login_required
@_admin_required
def diagnose_all():
    """Backfill diagnoses for all existing crash/timeout sessions."""
    count = backfill_diagnoses()
    return jsonify({'success': True, 'created': count})


@analytics_bp.route('/crashes/user/<discord_id>')
@login_required
@_admin_required
def user_crash_detail(discord_id):
    """Return full crash/timeout history for a specific player (admin view)."""
    from app.models import CrashDiagnosis
    now = datetime.now(timezone.utc)
    days = request.args.get('days', 30, type=int)
    since = now - timedelta(days=days)

    diagnoses = CrashDiagnosis.query.filter(
        CrashDiagnosis.discord_id == discord_id,
        CrashDiagnosis.detected_at >= since,
    ).order_by(CrashDiagnosis.detected_at.desc()).limit(50).all()

    # Summary counts
    crashes  = sum(1 for d in diagnoses if d.category == 'crash')
    timeouts = sum(1 for d in diagnoses if d.category == 'timeout')
    other    = sum(1 for d in diagnoses if d.category not in ('crash', 'timeout'))

    # Most common issue
    from collections import Counter
    key_counts = Counter(d.diagnosis_key for d in diagnoses)
    top_issue = key_counts.most_common(1)[0] if key_counts else None

    # Player name
    player_name = diagnoses[0].player_name if diagnoses else 'Unknown'

    return jsonify({
        'discord_id':   discord_id,
        'player_name':  player_name,
        'days':         days,
        'total':        len(diagnoses),
        'crashes':      crashes,
        'timeouts':     timeouts,
        'other':        other,
        'top_issue':    {'key': top_issue[0], 'count': top_issue[1]} if top_issue else None,
        'diagnoses':    [d.to_dict() for d in diagnoses],
    })


@analytics_bp.route('/crashes/profile/<discord_id>')
@login_required
def profile_crash_history(discord_id):
    """
    Player-facing crash history endpoint.
    Only the owner (matched via current_user.discord_id) or an admin can view.
    Marks records as viewed for the user.
    """
    from app.models import CrashDiagnosis
    # Auth check
    if not current_user.is_admin:
        if not current_user.discord_id or current_user.discord_id != discord_id:
            from flask import abort as _abort
            _abort(403)

    now   = datetime.now(timezone.utc)
    since = now - timedelta(days=30)

    diagnoses = CrashDiagnosis.query.filter(
        CrashDiagnosis.discord_id == discord_id,
        CrashDiagnosis.detected_at >= since,
    ).order_by(CrashDiagnosis.detected_at.desc()).limit(30).all()

    unviewed = sum(1 for d in diagnoses if not d.is_viewed_user)

    # Mark all as viewed
    for d in diagnoses:
        d.is_viewed_user = True
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify({
        'total':    len(diagnoses),
        'unviewed': unviewed,
        'diagnoses': [d.to_dict() for d in diagnoses],
    })


@analytics_bp.route('/data/crash-live')
@login_required
@_admin_required
def data_crash_live():
    """
    Lightweight live-update endpoint for the crashes page.
    Returns only the stats and recent incidents that change frequently.
    """
    from app.models import CrashDiagnosis
    now = datetime.now(timezone.utc)
    seven_days = now - timedelta(days=7)
    one_hour   = now - timedelta(hours=1)

    crashes_7d  = PlayerSession.query.filter(PlayerSession.join_time >= seven_days, PlayerSession.disconnect_category == 'crash').count()
    timeouts_7d = PlayerSession.query.filter(PlayerSession.join_time >= seven_days, PlayerSession.disconnect_category == 'timeout').count()
    network_7d  = PlayerSession.query.filter(PlayerSession.join_time >= seven_days, PlayerSession.disconnect_category == 'connection').count()
    total       = crashes_7d + timeouts_7d + network_7d

    # Last hour counts for "live" feel
    crashes_1h  = PlayerSession.query.filter(PlayerSession.join_time >= one_hour, PlayerSession.disconnect_category == 'crash').count()
    timeouts_1h = PlayerSession.query.filter(PlayerSession.join_time >= one_hour, PlayerSession.disconnect_category == 'timeout').count()

    # Recent 20 incidents
    recent = PlayerSession.query.filter(
        PlayerSession.disconnect_category.in_(['crash', 'timeout', 'connection']),
    ).order_by(PlayerSession.join_time.desc()).limit(20).all()

    # Unviewed diagnoses count (for notification badge)
    undiagnosed = CrashDiagnosis.query.filter_by(is_viewed_admin=False).count()

    return jsonify({
        'crashes_7d':   crashes_7d,
        'timeouts_7d':  timeouts_7d,
        'network_7d':   network_7d,
        'total':        total,
        'crashes_1h':   crashes_1h,
        'timeouts_1h':  timeouts_1h,
        'undiagnosed':  undiagnosed,
        'server_health': 'good' if total < 10 else ('warning' if total < 30 else 'critical'),
        'last_updated': now.isoformat(),
        'recent_incidents': [
            {
                'player':   s.player_name or 'Unknown',
                'discord_id': s.discord_id,
                'category': s.disconnect_category,
                'reason':   s.disconnect_reason or '',
                'ping':     s.disconnect_ping or 0,
                'duration': s.duration_seconds or 0,
                'time':     s.join_time.strftime('%d %b %H:%M'),
                'time_iso': s.join_time.isoformat(),
            }
            for s in recent
        ],
    })
