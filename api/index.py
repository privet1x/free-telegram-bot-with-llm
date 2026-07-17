"""Vercel entrypoint.

Vercel auto-detects FastAPI and serves the instance named ``app``. The actual app
assembly lives in app/server.py.
"""
from app.server import app  # noqa: F401
