"""
GSRP Discord Live Leaderboard Bot
Reads from the web app's database (same models).
Updates a pinned embed in a Discord channel every 30 seconds.

Setup:
  Add to .env:
    LEADERBOARD_CHANNEL_ID=your_channel_id_here

Run:
  Starts automatically alongside the web app: python run.py
  Or run it on its own:                       python discord_bot.py
"""

import discord
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# ── Path setup so we can import the Flask app ──────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# This bot reads its own settings straight from .env — it does NOT go
# through app.config / app/config.py. That keeps it working the same way
# whether it's started via run.py or on its own, regardless of how the
# Flask app's config loading is set up.
#
# override=True: if something else (systemd, Docker, a hosting panel) has
# already set these as empty/stale env vars, .env wins instead of being
# silently ignored.
ENV_PATH = os.path.join(BASE_DIR, '.env')
if not os.path.isfile(ENV_PATH):
    print(f'[GSRP Bot] WARNING: no .env file found at {ENV_PATH}')
else:
    load_dotenv(ENV_PATH, override=True)
    print(f'[GSRP Bot] Loaded .env from {ENV_PATH}')

UPDATE_INTERVAL  = 30   # seconds

MSG_ID_FILE = os.path.join(BASE_DIR, '.leaderboard_msg_id')

from app.models import db, PlayerSession, PlayerHeartbeat, PlayerStat, User

# flask_app is still needed for DB access (configure() below just points the
# bot at an already-created Flask app for that). BOT_TOKEN / CHANNEL_ID are
# read directly from .env, not from the Flask app's config.
flask_app  = None
BOT_TOKEN  = os.getenv('DISCORD_BOT_TOKEN', '')
CHANNEL_ID_RAW = os.getenv('LEADERBOARD_CHANNEL_ID', '')
CHANNEL_ID = int(CHANNEL_ID_RAW) if CHANNEL_ID_RAW.strip().isdigit() else 0
print(f'[GSRP Bot] LEADERBOARD_CHANNEL_ID read as: {CHANNEL_ID_RAW!r} -> {CHANNEL_ID}')




def configure(app):
    """Point the bot at an already-created Flask app (for DB access only)."""
    global flask_app
    flask_app = app

# ── Helpers ────────────────────────────────────────────────────────────

MEDALS = ['🥇', '🥈', '🥉']

# Weapon → emoji mapping (extend as needed)
WEAPON_ICONS = {
    'pistol': '🔫', 'rifle': '🎯', 'shotgun': '💥', 'knife': '🔪',
    'sniper': '🎯', 'smg': '🔫', 'grenade': '💣', 'fist': '👊',
}

def weapon_icon(weapon: str) -> str:
    w = (weapon or '').lower()
    for key, icon in WEAPON_ICONS.items():
        if key in w:
            return icon
    return '🔫'

def fmt(seconds):
    """Format seconds → '2d 3h 15m'"""
    if not seconds or seconds < 0:
        return '0m'
    seconds = int(seconds)
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    parts = []
    if d: parts.append(f'{d}d')
    if h: parts.append(f'{h}h')
    if m or not parts: parts.append(f'{m}m')
    return ' '.join(parts)


def get_username(discord_id):
    user = User.query.filter_by(discord_id=discord_id).first()
    if user:
        return user.username
    hb = PlayerHeartbeat.query.filter_by(discord_id=discord_id).first()
    if hb and hb.player_name:
        return hb.player_name
    s = PlayerSession.query.filter_by(discord_id=discord_id).order_by(
        PlayerSession.join_time.desc()).first()
    return s.player_name if s and s.player_name else f'<@{discord_id}>'


def fetch_data():
    with flask_app.app_context():
        try:
            now = datetime.now(timezone.utc)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            week_ago    = now - timedelta(days=7)

            # ── Live players ──────────────────────────────────────────
            cutoff  = now - timedelta(seconds=90)
            live_hb = PlayerHeartbeat.query.filter(
                PlayerHeartbeat.last_seen >= cutoff
            ).all()

            active_players = []
            for hb in live_hb:
                session = PlayerSession.query.filter_by(
                    discord_id=hb.discord_id
                ).filter(PlayerSession.leave_time.is_(None)).order_by(
                    PlayerSession.join_time.desc()
                ).first()

                session_secs = 0
                if session:
                    jt = session.join_time
                    if jt.tzinfo is None:
                        jt = jt.replace(tzinfo=timezone.utc)
                    session_secs = max(0, int((now - jt).total_seconds()))

                name = get_username(hb.discord_id)
                active_players.append({
                    'name': name,
                    'session_seconds': session_secs,
                    'cash': hb.cash or 0,
                    'bank': hb.bank or 0,
                    'ping': hb.ping or 0,
                })

            active_players.sort(key=lambda x: x['session_seconds'], reverse=True)

            # ── Server summary ────────────────────────────────────────
            unique_today = db.session.query(
                db.func.count(db.func.distinct(PlayerSession.discord_id))
            ).filter(PlayerSession.join_time >= today_start).scalar() or 0

            total_pt_today = db.session.query(
                db.func.coalesce(db.func.sum(PlayerSession.duration_seconds), 0)
            ).filter(
                PlayerSession.join_time >= today_start,
                PlayerSession.duration_seconds.isnot(None)
            ).scalar() or 0

            total_pt_week = db.session.query(
                db.func.coalesce(db.func.sum(PlayerSession.duration_seconds), 0)
            ).filter(
                PlayerSession.join_time >= week_ago,
                PlayerSession.duration_seconds.isnot(None)
            ).scalar() or 0

            kills_week = PlayerStat.query.filter(
                PlayerStat.event_type == 'kill',
                PlayerStat.recorded_at >= week_ago
            ).count()

            deaths_week = PlayerStat.query.filter(
                PlayerStat.event_type == 'death',
                PlayerStat.recorded_at >= week_ago
            ).count()

            # ── Today playtime leaderboard ────────────────────────────
            today_rows = db.session.query(
                PlayerSession.discord_id,
                PlayerSession.player_name,
                db.func.sum(PlayerSession.duration_seconds).label('total')
            ).filter(
                PlayerSession.join_time >= today_start,
                PlayerSession.duration_seconds.isnot(None)
            ).group_by(PlayerSession.discord_id, PlayerSession.player_name)\
             .order_by(db.func.sum(PlayerSession.duration_seconds).desc())\
             .limit(5).all()

            today_lb = [{'name': get_username(r.discord_id), 'seconds': int(r.total or 0)} for r in today_rows]

            # ── Weekly playtime leaderboard ───────────────────────────
            week_rows = db.session.query(
                PlayerSession.discord_id,
                PlayerSession.player_name,
                db.func.sum(PlayerSession.duration_seconds).label('total')
            ).filter(
                PlayerSession.join_time >= week_ago,
                PlayerSession.duration_seconds.isnot(None)
            ).group_by(PlayerSession.discord_id, PlayerSession.player_name)\
             .order_by(db.func.sum(PlayerSession.duration_seconds).desc())\
             .limit(5).all()

            week_lb = [{'name': get_username(r.discord_id), 'seconds': int(r.total or 0)} for r in week_rows]

            # ── Kill leaderboard ──────────────────────────────────────
            kill_rows = db.session.query(
                PlayerStat.discord_id,
                db.func.count(PlayerStat.id).label('kills')
            ).filter(
                PlayerStat.event_type == 'kill',
                PlayerStat.recorded_at >= week_ago
            ).group_by(PlayerStat.discord_id)\
             .order_by(db.func.count(PlayerStat.id).desc())\
             .limit(5).all()

            kill_lb = []
            for r in kill_rows:
                deaths = PlayerStat.query.filter_by(
                    discord_id=r.discord_id, event_type='death'
                ).filter(PlayerStat.recorded_at >= week_ago).count()
                kd = round(r.kills / max(deaths, 1), 2)
                kill_lb.append({
                    'name':   get_username(r.discord_id),
                    'kills':  r.kills,
                    'deaths': deaths,
                    'kd':     kd,
                })

            # ── Recent kills ──────────────────────────────────────────
            recent_kills = PlayerStat.query.filter_by(event_type='kill')\
                .order_by(PlayerStat.recorded_at.desc()).limit(5).all()

            kill_feed = [{
                'killer': k.killer_name or get_username(k.discord_id),
                'victim': k.victim_name or '?',
                'weapon': k.weapon or '?',
            } for k in recent_kills]

            return {
                'active_players':    active_players,
                'online_count':      len(active_players),
                'unique_today':      unique_today,
                'total_pt_today':    total_pt_today,
                'total_pt_week':     total_pt_week,
                'kills_week':        kills_week,
                'deaths_week':       deaths_week,
                'today_lb':          today_lb,
                'week_lb':           week_lb,
                'kill_lb':           kill_lb,
                'kill_feed':         kill_feed,
            }
        except Exception as e:
            print(f'[ERROR] fetch_data: {e}')
            import traceback; traceback.print_exc()
            return None


def build_embed(data):
    now = datetime.now(timezone.utc)

    # Dynamic color: green when players online, purple when empty
    color = 0x57F287 if data['online_count'] > 0 else 0x6366F1

    embed = discord.Embed(
        title='🎮  GSRP  ·  Live Server Dashboard',
        color=color,
        timestamp=now,
    )

    # ── Header summary bar ────────────────────────────────────────────
    kd_ratio = round(data['kills_week'] / max(data['deaths_week'], 1), 2)
    embed.description = (
        f'`🟢 {data["online_count"]:>2} Online` '
        f'`👥 {data["unique_today"]:>3} Today` '
        f'`⏱ {fmt(data["total_pt_today"])} Playtime` '
        f'`⚔️ {data["kills_week"]} Kills`'
    )

    # ── Live players ──────────────────────────────────────────────────
    if data['active_players']:
        lines = []
        for p in data['active_players'][:15]:
            ping_bar = '🟢' if p['ping'] < 80 else ('🟡' if p['ping'] < 150 else '🔴')
            lines.append(
                f'{ping_bar} **{p["name"]}**'
                f'  `⏱ {fmt(p["session_seconds"])}`'
                f'  `💵 ${p["cash"]:,}`'
                f'  `🏦 ${p["bank"]:,}`'
                f'  `{p["ping"]}ms`'
            )
        player_block = '\n'.join(lines)
        embed.add_field(
            name=f'━━  🔴  IN-GAME NOW  ({data["online_count"]})  ━━',
            value=player_block,
            inline=False,
        )
    else:
        embed.add_field(
            name='━━  🔴  IN-GAME NOW  (0)  ━━',
            value='> *No players currently online.*',
            inline=False,
        )

    # ── Spacer ────────────────────────────────────────────────────────
    embed.add_field(name='\u200b', value='\u200b', inline=False)

    # ── Today leaderboard ─────────────────────────────────────────────
    if data['today_lb']:
        lines = []
        for i, p in enumerate(data['today_lb']):
            medal = MEDALS[i] if i < 3 else f'`#{i+1}`'
            lines.append(f'{medal}  **{p["name"]}** — `{fmt(p["seconds"])}`')
        embed.add_field(
            name='📊  TODAY\'S PLAYTIME',
            value='\n'.join(lines),
            inline=True,
        )
    else:
        embed.add_field(name='📊  TODAY\'S PLAYTIME', value='*No data yet*', inline=True)

    # ── Weekly leaderboard ────────────────────────────────────────────
    if data['week_lb']:
        lines = []
        for i, p in enumerate(data['week_lb']):
            medal = MEDALS[i] if i < 3 else f'`#{i+1}`'
            lines.append(f'{medal}  **{p["name"]}** — `{fmt(p["seconds"])}`')
        embed.add_field(
            name='📈  WEEKLY PLAYTIME',
            value='\n'.join(lines),
            inline=True,
        )
    else:
        embed.add_field(name='📈  WEEKLY PLAYTIME', value='*No data yet*', inline=True)

    # ── Kill leaderboard ──────────────────────────────────────────────
    if data['kill_lb']:
        lines = []
        for i, p in enumerate(data['kill_lb']):
            medal = MEDALS[i] if i < 3 else f'`#{i+1}`'
            lines.append(
                f'{medal}  **{p["name"]}**'
                f'  `{p["kills"]}K / {p["deaths"]}D`'
                f'  `{p["kd"]} K/D`'
            )
        embed.add_field(
            name='⚔️  WEEKLY KILL LEADERS',
            value='\n'.join(lines),
            inline=False,
        )
    else:
        embed.add_field(
            name='⚔️  WEEKLY KILL LEADERS',
            value='*No kills recorded yet.*',
            inline=False,
        )

    # ── Recent kill feed ──────────────────────────────────────────────
    if data['kill_feed']:
        lines = []
        for k in data['kill_feed']:
            icon = weapon_icon(k['weapon'])
            lines.append(
                f'{icon}  **{k["killer"]}** `→` {k["victim"]}'
                f'  *via* `{k["weapon"]}`'
            )
        embed.add_field(
            name='💀  RECENT KILL FEED',
            value='\n'.join(lines),
            inline=False,
        )

    # ── Footer ────────────────────────────────────────────────────────
    embed.set_footer(
        text=(
            f'GSRP Leaderboard  •  Updates every 30s  •  '
            f'Weekly: {data["kills_week"]}K / {data["deaths_week"]}D  •  '
            f'{fmt(data["total_pt_week"])} total playtime'
        )
    )

    return embed


# ── Message ID persistence ─────────────────────────────────────────────

def save_msg_id(mid):
    with open(MSG_ID_FILE, 'w') as f:
        f.write(str(mid))

def load_msg_id():
    try:
        with open(MSG_ID_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


# ── Discord bot ────────────────────────────────────────────────────────
#
# This is the one bot for the whole site: it posts/updates the live
# leaderboard dashboard below (same as the old leaderboard-only bot), AND
# — via app/utils.py's discord_api_request/add_discord_role/_join_guild
# etc. — its token (DISCORD_BOT_TOKEN) is what the Flask app itself uses
# for Discord OAuth guild-join and role sync. It also carries a small set
# of category-scoped admin slash commands (see below) for channel
# management in exactly one category — never anywhere else, enforced in
# code regardless of what server permissions the bot has been granted.

import json as _json_dbot
import logging as _logging_dbot
from discord import app_commands
from discord.ext import commands

ALLOWED_CATEGORY_ID = int(os.getenv('ALLOWED_CATEGORY_ID', '1566556666332324052'))
DISCORD_GUILD_ID_RAW = os.getenv('DISCORD_GUILD_ID', '')
DISCORD_GUILD_ID = int(DISCORD_GUILD_ID_RAW) if DISCORD_GUILD_ID_RAW.strip().isdigit() else 0

_BACKUP_DIR = os.path.join(BASE_DIR, 'discord_backups')
os.makedirs(_BACKUP_DIR, exist_ok=True)

_bot_log = _logging_dbot.getLogger('gsrp.discord_bot')
_bot_log.addHandler(_logging_dbot.FileHandler(os.path.join(BASE_DIR, 'discord_bot_commands.log')))
_bot_log.setLevel(_logging_dbot.INFO)

intents = discord.Intents.default()
bot     = commands.Bot(command_prefix='!unused-', intents=intents)
client  = bot  # kept for anything still referencing the old name
lb_msg  = None


# ── Category guard + backup (same logic as rps-category-bot, folded in) ─

class NotInAllowedCategory(Exception):
    pass


def _require_channel_in_category(channel):
    if channel.category_id != ALLOWED_CATEGORY_ID:
        raise NotInAllowedCategory(
            f'#{channel.name} is not inside the allowed category (id {ALLOWED_CATEGORY_ID}) — refusing.'
        )


def _overwrites_to_dict(channel):
    out = {}
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        kind = 'role' if isinstance(target, discord.Role) else 'member'
        out[f'{kind}:{target.id}:{target.name}'] = {
            'allow': [p for p, v in allow if v],
            'deny':  [p for p, v in deny if v],
        }
    return out


def backup_guild(guild) -> str:
    """Full snapshot of every role and channel — written before ANY destructive command runs."""
    snapshot = {
        'guild_id': guild.id,
        'guild_name': guild.name,
        'taken_at': datetime.now(timezone.utc).isoformat(),
        'roles': [
            {
                'id': r.id, 'name': r.name, 'color': str(r.color), 'position': r.position,
                'hoist': r.hoist, 'mentionable': r.mentionable, 'managed': r.managed,
                'permissions': r.permissions.value,
            }
            for r in guild.roles
        ],
        'channels': [
            {
                'id': c.id, 'name': c.name, 'type': str(c.type), 'position': c.position,
                'category_id': c.category_id, 'category_name': c.category.name if c.category else None,
                'topic': getattr(c, 'topic', None), 'nsfw': getattr(c, 'nsfw', None),
                'overwrites': _overwrites_to_dict(c),
            }
            for c in guild.channels
        ],
    }
    ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    path = os.path.join(_BACKUP_DIR, f'backup_{guild.id}_{ts}.json')
    with open(path, 'w') as fh:
        _json_dbot.dump(snapshot, fh, indent=2)
    _bot_log.info('Backup written: %s (%d roles, %d channels)', path, len(snapshot['roles']), len(snapshot['channels']))
    return path


class ConfirmDelete(discord.ui.View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=30)
        self.invoker_id = invoker_id
        self.confirmed = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message('Only the person who ran this command can confirm it.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Delete channel', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


@bot.tree.command(name='category_status', description='List the channels currently in the managed category')
@app_commands.default_permissions(manage_channels=True)
async def category_status(interaction: discord.Interaction):
    category = interaction.guild.get_channel(ALLOWED_CATEGORY_ID)
    if not category or not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message(
            f'Category `{ALLOWED_CATEGORY_ID}` was not found in this server — nothing for me to manage here.',
            ephemeral=True,
        )
        return
    lines = [f'**{category.name}** (`{category.id}`) — {len(category.channels)} channel(s):']
    for ch in category.channels:
        lines.append(f'  • {getattr(ch, "mention", ch.name)} (`{ch.id}`, {ch.type})')
    await interaction.response.send_message('\n'.join(lines), ephemeral=True)


@bot.tree.command(name='category_backup', description='Take a manual backup of all roles and channels right now')
@app_commands.default_permissions(manage_channels=True)
async def category_backup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    path = backup_guild(interaction.guild)
    await interaction.followup.send(f'Backup written: `{os.path.basename(path)}`', ephemeral=True)


@bot.tree.command(name='category_rename', description='Rename a channel — only works inside the managed category')
@app_commands.describe(channel='The channel to rename', new_name='The new channel name')
@app_commands.default_permissions(manage_channels=True)
async def category_rename(interaction: discord.Interaction, channel: discord.abc.GuildChannel, new_name: str):
    try:
        _require_channel_in_category(channel)
    except NotInAllowedCategory as e:
        _bot_log.warning('REJECTED rename by %s: %s', interaction.user, e)
        await interaction.response.send_message(f'🚫 {e}', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    backup_guild(interaction.guild)
    old_name = channel.name
    await channel.edit(name=new_name, reason=f'/category_rename by {interaction.user} ({interaction.user.id})')
    _bot_log.info('RENAMED #%s -> #%s by %s', old_name, new_name, interaction.user)
    await interaction.followup.send(f'✅ Renamed **#{old_name}** → **#{new_name}**. Backup taken first.', ephemeral=True)


@bot.tree.command(name='category_archive', description='Lock + archive a channel — only works inside the managed category')
@app_commands.describe(channel='The channel to archive')
@app_commands.default_permissions(manage_channels=True)
async def category_archive(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    try:
        _require_channel_in_category(channel)
    except NotInAllowedCategory as e:
        _bot_log.warning('REJECTED archive by %s: %s', interaction.user, e)
        await interaction.response.send_message(f'🚫 {e}', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    backup_guild(interaction.guild)
    everyone = interaction.guild.default_role
    overwrite = channel.overwrites_for(everyone)
    overwrite.send_messages = False
    overwrite.connect = False
    await channel.set_permissions(everyone, overwrite=overwrite, reason=f'/category_archive by {interaction.user} ({interaction.user.id})')
    new_name = channel.name if channel.name.startswith('archived-') else f'archived-{channel.name}'
    if new_name != channel.name:
        await channel.edit(name=new_name, reason=f'/category_archive by {interaction.user} ({interaction.user.id})')
    _bot_log.info('ARCHIVED #%s by %s', channel.name, interaction.user)
    await interaction.followup.send(f'✅ Archived **#{new_name}** (locked for @everyone). Backup taken first.', ephemeral=True)


@bot.tree.command(name='category_cleanup', description='Bulk-delete recent messages in a channel — only works inside the managed category')
@app_commands.describe(channel='The channel to clean up', limit='How many recent messages to delete (max 100, default 50)')
@app_commands.default_permissions(manage_channels=True)
async def category_cleanup(interaction: discord.Interaction, channel: discord.TextChannel, limit: app_commands.Range[int, 1, 100] = 50):
    try:
        _require_channel_in_category(channel)
    except NotInAllowedCategory as e:
        _bot_log.warning('REJECTED cleanup by %s: %s', interaction.user, e)
        await interaction.response.send_message(f'🚫 {e}', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    backup_guild(interaction.guild)
    deleted = await channel.purge(limit=limit, reason=f'/category_cleanup by {interaction.user} ({interaction.user.id})')
    _bot_log.info('CLEANUP #%s: deleted %d message(s) by %s', channel.name, len(deleted), interaction.user)
    note = '' if len(deleted) == limit else ' (fewer than requested were deleted — Discord only bulk-deletes messages younger than 14 days)'
    await interaction.followup.send(f'✅ Deleted {len(deleted)} message(s) in **#{channel.name}**{note}. Backup taken first.', ephemeral=True)


@bot.tree.command(name='category_delete', description='Permanently delete a channel — only works inside the managed category')
@app_commands.describe(channel='The channel to delete')
@app_commands.default_permissions(manage_channels=True)
async def category_delete(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    try:
        _require_channel_in_category(channel)
    except NotInAllowedCategory as e:
        _bot_log.warning('REJECTED delete by %s: %s', interaction.user, e)
        await interaction.response.send_message(f'🚫 {e}', ephemeral=True)
        return
    view = ConfirmDelete(interaction.user.id)
    await interaction.response.send_message(
        f'⚠️ Really delete **#{channel.name}** (`{channel.id}`)? A backup will be taken first, but there is no '
        f'Discord undo — recovering means manually recreating the channel from that backup.',
        view=view,
        ephemeral=True,
    )
    await view.wait()
    if not view.confirmed:
        await interaction.edit_original_response(content='Cancelled — nothing was deleted.', view=None)
        return
    backup_guild(interaction.guild)
    name, cid = channel.name, channel.id
    await channel.delete(reason=f'/category_delete by {interaction.user} ({interaction.user.id})')
    _bot_log.info('DELETED #%s (%s) by %s', name, cid, interaction.user)
    await interaction.edit_original_response(content=f'✅ Deleted **#{name}**. Backup was taken first.', view=None)


# ── Leaderboard dashboard (unchanged behaviour, now on `bot` not `client`) ─

@bot.event
async def on_ready():
    global lb_msg
    print(f'[GSRP Bot] Logged in as {bot.user}')

    try:
        if DISCORD_GUILD_ID:
            synced = await bot.tree.sync(guild=discord.Object(id=DISCORD_GUILD_ID))
        else:
            synced = await bot.tree.sync()
        print(f'[GSRP Bot] Synced {len(synced)} slash command(s)')
    except Exception as e:
        print(f'[GSRP Bot] Slash command sync failed: {e}')

    if not CHANNEL_ID:
        print('[GSRP Bot] LEADERBOARD_CHANNEL_ID not set — skipping the leaderboard dashboard '
              '(slash commands still work).')
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f'[GSRP Bot] ERROR: Channel {CHANNEL_ID} not found. Check LEADERBOARD_CHANNEL_ID in .env')
        return

    # Try to recover existing message
    saved_id = load_msg_id()
    if saved_id:
        try:
            lb_msg = await channel.fetch_message(saved_id)
            print(f'[GSRP Bot] Recovered existing message {saved_id}')
        except Exception:
            lb_msg = None

    # Create new message if needed
    if not lb_msg:
        embed = discord.Embed(
            title='🎮  GSRP  ·  Live Server Dashboard',
            description='⏳ Loading data…',
            color=0x6366F1,
        )
        lb_msg = await channel.send(embed=embed)
        save_msg_id(lb_msg.id)
        print(f'[GSRP Bot] Created message {lb_msg.id}')

    asyncio.create_task(update_loop())


async def update_loop():
    global lb_msg
    await bot.wait_until_ready()
    print('[GSRP Bot] Update loop started')

    while not bot.is_closed():
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, fetch_data)

            if data and lb_msg:
                embed = build_embed(data)
                await lb_msg.edit(embed=embed)
                print(f'[GSRP Bot] Updated — {data["online_count"]} online')

        except discord.errors.NotFound:
            channel = bot.get_channel(CHANNEL_ID)
            if channel:
                data = await asyncio.get_event_loop().run_in_executor(None, fetch_data)
                if data:
                    embed = build_embed(data)
                    lb_msg = await channel.send(embed=embed)
                    save_msg_id(lb_msg.id)
                    print('[GSRP Bot] Recreated deleted message')

        except discord.errors.HTTPException as e:
            print(f'[GSRP Bot] HTTP error: {e}')

        except Exception as e:
            print(f'[GSRP Bot] Error: {e}')

        await asyncio.sleep(UPDATE_INTERVAL)


if __name__ == '__main__':
    from app import create_app
    configure(create_app())

    if not BOT_TOKEN:
        print('[ERROR] DISCORD_BOT_TOKEN not set in .env')
        sys.exit(1)

    print(f'[GSRP Bot] Starting — leaderboard channel {CHANNEL_ID or "(none configured)"} '
          f'— managed category {ALLOWED_CATEGORY_ID} — updating every {UPDATE_INTERVAL}s')
    bot.run(BOT_TOKEN, log_handler=None)