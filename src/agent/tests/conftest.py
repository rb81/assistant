import importlib


for dependency in ("yaml", "psycopg.rows", "psycopg.types.json", "psycopg.conninfo", "assistant_agent.notifications"):
    importlib.import_module(dependency)


def complete_model_settings(values, model):
    for name in (
        "AGENT_NAMING_MODEL", "AGENT_ROUTING_MODEL", "AGENT_LIMITS_SUMMARIZATION_MODEL",
        "AGENT_MEMORY_STEWARD_MODEL", "AGENT_DEEP_RESEARCH_MODEL", "AGENT_DEEP_RESEARCH_SEARCH_MODEL",
        "AGENT_SEARCH_MODEL", "AGENT_SECURITY_DEVELOPMENT_MODEL",
    ):
        values.setdefault(name, model)