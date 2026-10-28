"""Entry point for production servers:  gunicorn wsgi:app"""

from web import create_app

app = create_app()
