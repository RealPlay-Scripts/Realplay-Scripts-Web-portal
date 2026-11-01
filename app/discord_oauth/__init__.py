from flask import Blueprint
discord_bp = Blueprint('discord_oauth', __name__)
from app.discord_oauth import routes
