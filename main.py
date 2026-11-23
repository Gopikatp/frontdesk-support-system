"""Compatibility entry point for existing `uvicorn main:app` commands."""

from backend.main import app

__all__ = ["app"]
