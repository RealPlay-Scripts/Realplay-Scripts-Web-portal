from flask import Blueprint
applications_bp = Blueprint('applications', __name__)
from app.applications import routes
