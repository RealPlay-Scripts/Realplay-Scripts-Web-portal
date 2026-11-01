import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Core
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///whitelist.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_recycle': 300,
        'pool_pre_ping': True,
    }

    # Session
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)

    # Discord
    DISCORD_CLIENT_ID = os.environ.get('DISCORD_CLIENT_ID', '')
    DISCORD_CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET', '')
    DISCORD_REDIRECT_URI = os.environ.get('DISCORD_REDIRECT_URI', 'http://localhost:5000/auth/discord/callback')
    DISCORD_BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
    DISCORD_GUILD_ID = os.environ.get('DISCORD_GUILD_ID', '1213807920635052043')
    ECONOMY_FLAGS_WEBHOOK = os.environ.get('ECONOMY_FLAGS_WEBHOOK', 'https://discord.com/api/webhooks/1492963668508279058/cQd44aQWewAJOSV7aXKZRk3JSYwdNC2-GBUa2bpUBRhHlRhpmMBsVXyPd0vDXDGgyycv')
    DISCORD_API_BASE = 'https://discord.com/api/v10'
    DISCORD_OAUTH_URL = 'https://discord.com/api/oauth2/authorize'
    DISCORD_TOKEN_URL = 'https://discord.com/api/oauth2/token'

    # FiveM
    FIVEM_API_KEY = os.environ.get('FIVEM_API_KEY', '')

    # API
    API_KEY_SECRET = os.environ.get('API_KEY_SECRET', 'api-secret-change-me')
    API_RATE_LIMIT = '100 per hour'

    # Mail
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'localhost')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'True').lower() == 'true'
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME', '')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD', '')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_USERNAME', 'noreply@cfrp.co.za')

    # Uploads
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'app', 'static', 'uploads')
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'}

    # Site
    SITE_NAME = os.environ.get('SITE_NAME', 'CFRP Whitelist')
    SITE_URL = os.environ.get('SITE_URL', 'http://localhost:5000')

    # Cache
    CACHE_TYPE = 'SimpleCache'
    CACHE_DEFAULT_TIMEOUT = 20000

    # 2FA
    TOTP_ISSUER = os.environ.get('TOTP_ISSUER', 'CFRP Whitelist')

    # Redis
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

    # Heartbeat timeout (seconds before player is considered offline)
    HEARTBEAT_TIMEOUT = 90


class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_ECHO = False


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_size': 10,
        'max_overflow': 20,
        'pool_recycle': 300,
        'pool_pre_ping': True,
    }


config_map = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig,
}

def get_config():
    env = os.environ.get('FLASK_ENV', 'development')
    return config_map.get(env, DevelopmentConfig)
BOT_WEBHOOK_URL = os.environ.get('BOT_WEBHOOK_URL', 'http://localhost:5001')
WEBHOOK_SECRET  = os.environ.get('WEBHOOK_SECRET', '')
INTERVIEW_CHANNEL_ID = os.environ.get('INTERVIEW_CHANNEL_ID', '')