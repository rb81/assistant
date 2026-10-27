import importlib


for dependency in ("yaml", "psycopg.rows", "psycopg.types.json", "psycopg.conninfo", "assistant_agent.notifications"):
    importlib.import_module(dependency)