from flask import Blueprint

interviews_bp = Blueprint('interviews', __name__)

from app.interviews import routes  # noqa
