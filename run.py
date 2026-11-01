import os
import threading

from app import create_app

app = create_app()


def _start_discord_bot(flask_app):
    """Run the Discord bot (leaderboard dashboard + category-scoped admin
    slash commands) in the background, sharing this process's Flask
    app/database instead of spinning up a second one. Skips silently if no
    bot token is configured — the web app still works fine without it.

    NOTE: this is a dev convenience for `python run.py` only. In
    production (gunicorn, multiple workers) each worker would try to log
    in with the same token and collide — the live deployment runs the bot
    as its own separate systemd service instead, not through here."""
    # discord_bot.py loads .env itself and reads BOT_TOKEN / CHANNEL_ID
    # straight from it — not from flask_app.config — so we check the same
    # source here rather than app.config, which may be populated
    # differently (or not at all) by app/config.py.
    import discord_bot

    if not discord_bot.BOT_TOKEN:
        print('[Bot] DISCORD_BOT_TOKEN not set — skipping the Discord bot. '
              'The web app will still run fine.')
        return

    discord_bot.configure(flask_app)

    def _run():
        try:
            discord_bot.bot.run(discord_bot.BOT_TOKEN, log_handler=None)
        except Exception as exc:
            print(f'[Bot] Discord bot stopped: {exc}')

    thread = threading.Thread(target=_run, daemon=True, name='discord-bot')
    thread.start()
    print('[Bot] Discord bot starting in the background…')


if __name__ == '__main__':
    # With Flask's debug reloader on, this file is imported twice (once by
    # the reloader's watcher process, once by the actual server process).
    # Only start the bot in the process that's really serving requests, or
    # it'll try to log in to Discord with the same token twice.
    is_reloader_child = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    if is_reloader_child or not app.debug:
        _start_discord_bot(app)

    app.run(debug=True, host='0.0.0.0', port=5010)
