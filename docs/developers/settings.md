# Add or change an Assistant setting

This checklist is for developers and coding agents. The server owns installation settings; the TUI and macOS app are clients of its Management API. Do not invent a separate client-side setting, write installation files from either client, or mark the change complete after editing only the runtime.

## 1. Define the setting and wire the runtime

- For application settings, add the leaf and its shipped default to [`src/config/agent.yaml`](../../src/config/agent.yaml). `settings_schema.py` derives the environment name (`agent.search.enabled` → `AGENT_SEARCH_ENABLED`) and normally infers the type. Don't add a parallel name map. Keep any runtime fallback consistent with the shipped default.
- Add the matching dotted path to [`src/config/agent.schema.yaml`](../../src/config/agent.schema.yaml). Supply a human-readable label and help describing the default or blank behavior, prerequisites, and consequential costs, data sharing, or activation requirements. Declare the type when a null default would make it ambiguous. Mark credentials `secret: true` and leave their shipped value blank. An unmatched metadata entry is ignored; the sidecar does not define settings.
- Use the typed `AppConfig` accessors and cover the setting wherever behavior, validation, or capability discovery depends on it. Types are not semantic validation: enforce supported choices, ranges, and cross-field constraints in setup draft validation and wherever hand-edited overrides or deployed runtime configuration can otherwise bypass that validation.
- Keep bootstrap/Compose settings in the deployment registry and `data/.env`, not in application overrides. Ordinary settings persist to `data/overrides.env`. Credentials use `secret_store.py` and setup's scoped Compose `_FILE` grants; grant only consuming services and verify host-directory and Docker-volume storage. Don't put secrets in YAML defaults, examples, saved drafts, API responses, logs, or diagnostics. If the setting affects service installation or activation, check Compose profiles, configuration checks, and apply/recreate behavior as well. See [configuration precedence](../env-overrides.md) and [credential storage](../security.md#docker-secrets).

## 2. Verify the Management API contract and both settings interfaces

- In `management_api.py`, verify `configuration_groups`, `configuration_group_details`, and `setting_presentation`: the setting should appear once in the intended destination, group, section, and basic/advanced view. Leaves not explicitly placed fall into an advanced `catalog:*` group. Check `setup.py` if the bootstrap/setup flow also needs the setting, and `management_help.py` if shared help needs updating.
- Check the actual wire metadata, not just YAML. The sidecar's `enum` is not a substitute for the API's `metadata.choices` and server validation. The current API adds fixed choices and labels through its presentation registries and can derive choices dynamically (for example, configured calendars). Use stable values and readable labels. Make sure a choice only becomes selectable when its prerequisites are met.
- Read `GET /v2/configuration/catalog` for navigation and `GET /v2/configuration/settings?group=…` for revision-bound values. Catalog fields contain no effective values. Check that the setting is returned with the expected type, editability and help, and that credential presence can be read without returning its value. Use `POST /v2/configuration/preview` to review the exact draft; a confirmed `configuration.save` operation writes that reviewed draft. Test save-without-apply and, when relevant, revision-checked activation separately. Don't add a feature-specific configuration endpoint just to save a setting.
- **TUI:** Check `management_tui.py` and `management_client.py`. Confirm the field can be found, edited, reviewed, and reloaded through the API. For a standard field this should be driven by catalog metadata; change TUI code only if the existing renderer cannot support the interaction.
- **macOS:** In the separate [client repository](https://github.com/rb81/assistant-client), check `ClientModel.swift`, `Contracts.swift` (`SettingField`/`SettingGroup`), `SettingsNavigation.swift`, `ConfigurationView.swift`, and `SettingRow.swift`. Verify the intended destination, control, editability, credential status, preview, save, and activation behavior. Standard fields should render from metadata without a Swift change. For a new control or contract, update the renderer, preview data and affected tests; keep compatibility with older servers by using capability checks for newly required operations or resources.
- If you change the API contract rather than just add a setting, update `management_contracts.py`, the [API reference](../management-api.md), and the applicable coverage in `src/scripts/audit_management_api.py`. Don't duplicate the API's labels, validation, or settings layout in a client.

## 3. Test the behavior, not just the presence of the field

| Layer | Where to extend tests and what to prove |
|---|---|
| Catalog and runtime | `test_settings_schema.py` and the feature tests: defaults preserve existing behavior, a real override reaches the consumer, invalid values do not produce a successful response, and any dependent setting is handled correctly. |
| Setup and Management API | `test_setup.py`, `test_management_api.py`, and `test_client_modernization.py`: one focused catalog → section → preview → confirmed save → reload test; cover stale revision, invalid combination, and activation when changed. For credentials, check redaction on reads, review and errors, and least-privilege grants. |
| TUI | `test_management_tui.py` and `test_management_transport.py`: verify field presentation, editing and review when adding interaction or metadata the existing tests don't exercise. Use API fixtures, not a bypass through local file writes. |
| macOS | `Assistant/AssistantTests/ModernizationTests.swift` and the fake `ChatAPI.swift`: test new decoding/navigation/editor behavior and the outbound preview/save contract. Build and run affected tests with Xcode; verify the settings page against a server or a representative catalog fixture. |
| Upgrade | `test_migrations.py` or the database migration suite: run against representative old data, assert the effective settings and preserved values, then re-run to prove safe replay where applicable. |

From the server repository, using the development Python environment:

```bash
export PYTHONPATH=src/agent/src
python src/scripts/audit_settings.py
python src/scripts/audit_management_api.py
python -m pytest -q src/agent/tests/test_settings_schema.py
```

Also run the targeted feature, setup, API, and client tests that cover the changed behavior. Audits are useful consistency checks, not a substitute for a complete save/reload test.

## 4. Decide how older installations upgrade

- **Additive default that preserves current behavior:** normally no migration; omitted overrides take the new shipped default. Don't copy all shipped defaults into local overrides.
- **Rename, remove, change representation, or change behavior for existing configurations:** decide explicitly whether to preserve the old effective value. If stored installation files or credential names must change, use the next numbered root `migrations/` script, run by `install.sh`. Follow the existing backup and conflict-handling patterns. Preserve explicit overrides and unrelated values, don't overwrite a conflicting destination value, and don't expose credentials in output. Test old, mixed, and already migrated installations. Document what happens to deployment-owned environment overrides, which a migration may not control.
- **Database state:** add the next packaged SQL migration under `src/agent/src/assistant_agent/migrations/` and update `src/database/schema.sql` and its applied records for fresh installs. Don't edit an already applied migration. `Database.run_migrations` serializes and records migrations by filename. Verify both a new database and an upgrade from supported existing state.
- **Contract changes:** prefer additive API fields. Don't rename an existing key or move an essential control without checking older clients and activation/reporting behavior. Update the operator guide, applicable API examples, and release/migration notes in the same change. Remove obsolete instructions rather than leaving two competing workflows.
