"""Read-only model overview derived from the shared settings snapshot."""
from . import settings_schema
from .security_screening import PROVIDERS, DEFAULT_PROVIDER


def overview(configuration, state):
    fields = {field["key"]: field for field in configuration["settings"]}
    pending = set(state.get("pending_configuration", []))
    groups = {group["id"]: group for group in configuration["groups"]}
    sections = []
    for descriptor in settings_schema.model_sections():
        selected = []
        for original in fields.values():
            if (original["metadata"].get("hidden")
                    or original["metadata"].get("model_section") != descriptor["id"]):
                continue
            field = dict(original)
            value = field["value"]
            source, inherited_from = "configured", None
            if field["secret"]:
                field["value"] = field["default"] = ""
                value = "Configured" if field["key"] in configuration["credential_names"] else "Not configured"
                source = "credential_status"
            elif not value.strip():
                parent = field["metadata"].get("model_inherits")
                if parent:
                    inherited_from = settings_schema.derived_env_name(parent)
                    inherited = fields[inherited_from]
                    value = inherited["value"] or "Not configured"
                    source = "inherited"
                elif field["key"] in {"AGENT_SECURITY_DECISION_MODEL_MODEL", "AGENT_SECURITY_DECISION_MODEL_URL"}:
                    provider = fields["AGENT_SECURITY_DECISION_MODEL_PROVIDER"]["value"] or DEFAULT_PROVIDER
                    spec = PROVIDERS.get(provider.strip().lower())
                    attribute = "model" if field["key"].endswith("_MODEL") else "url"
                    value = getattr(spec, attribute, "") or "Not configured"
                    source = "provider_default" if spec and getattr(spec, attribute) else "unset"
                else:
                    value = field["metadata"].get("model_blank_label") or "Not configured"
                    source = field["metadata"].get("model_blank_source", "configured") if field["metadata"].get("model_blank_label") else "unset"
            field.update(
                display_value=value, value_source=source, inherited_from=inherited_from,
                pending_activation=field["key"] in pending or inherited_from in pending
                or bool(pending & {"agent.yaml", "setup"}),
            )
            selected.append(field)
        if not selected:
            continue
        enabled_key = settings_schema.derived_env_name(descriptor["enabled"]) if descriptor.get("enabled") else None
        provider_key = settings_schema.derived_env_name(descriptor["provider"]) if descriptor.get("provider") else None
        provider = fields[provider_key]["value"] if provider_key else descriptor.get("provider_url", "")
        if provider in {"", "inherit", "auto"}:
            provider = "Not configured"
        available_keys = {field["key"] for field in selected}
        selection_keys = [settings_schema.derived_env_name(path) for path in descriptor.get("selectors", [])
                          if settings_schema.derived_env_name(path) in available_keys]
        related_settings = {}
        for field in selected:
            if field["key"] in selection_keys:
                continue
            owner = groups[field["group"]]
            root = groups.get(owner.get("parent"), owner)
            advanced = (root.get("destination") == "server" or not root["basic"]
                        or field["metadata"].get("level") == "advanced")
            destination = "advanced" if advanced else root.get("destination", "models")
            related_settings.setdefault((root["id"], destination), {
                "group": root["id"], "destination": destination,
                "title": (root.get("advanced_title") or root["title"]) if advanced else root["title"],
                "field_key": field["key"],
            })
        sections.append({
            "id": descriptor["id"], "title": descriptor["title"],
            "provider": provider or "Not configured",
            "enabled": fields[enabled_key]["value"] == "true" if enabled_key else None,
            "summary_keys": [settings_schema.derived_env_name(path) for path in descriptor.get("summary", [])
                             if settings_schema.derived_env_name(path) in available_keys],
            "selection_keys": selection_keys,
            "related_settings": list(related_settings.values()),
            "settings": selected,
        })
    return {
        "api_version": 2, "revision": configuration["revision"], "value_state": "saved",
        "activation_tracked": "pending_configuration" in state,
        "pending_activation": bool(pending), "sections": sections, "read_only": True,
    }
