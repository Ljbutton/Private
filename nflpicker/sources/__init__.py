"""Adapters for external data sources.

Every adapter returns plain dicts/dataclasses and never touches the database,
so each one is independently testable against a recorded fixture.
"""
