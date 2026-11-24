#!/usr/bin/env python3
"""Inventory management requirements and compare them with the v2 API contract.

Run with PYTHONPATH=src/agent/src python src/scripts/audit_management_api.py
or pass --json for machine-readable output. Contract presence is not proof of
runtime behavior; missing or unverified requirements make the audit fail.
"""
import argparse
import ast
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import get_args
from unittest.mock import Mock, patch
from xml.etree import ElementTree

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/agent/src"))

from assistant_agent import settings_schema  # noqa: E402
from assistant_agent.admin_commands import HELP  # noqa: E402
from assistant_agent.management import Management  # noqa: E402
from assistant_agent.management_api import DERIVED_SETTINGS, ManagementService, create_app  # noqa: E402
from assistant_agent.management_contracts import (  # noqa: E402
    ManagementOperationRequest, ManagementReadRequest, PermissionControlOperation, RecordChildrenRequest, RecordListRequest,
    RecordMutationOperation, RecordReadRequest,
    ServiceLifecycleOperation,
)
from assistant_agent.management_operator import COLLECTIONS, MUTATIONS  # noqa: E402
from assistant_agent.management_domains import (  # noqa: E402
    PROJECT_ARGUMENTS, READ_ACTIONS, WRITE_ACTIONS, ReadAction, WriteAction, catalog as domain_catalog,
)
from assistant_agent.secret_store import is_secret_name  # noqa: E402
from assistant_agent.setup import SetupError  # noqa: E402


RUNTIME_INTERNAL = {
    "AGENT_CONFIG": "Configuration file path selected by the managed installation and config checker.",
    "AGENT_ROLE": "Per-service worker role selected by Compose.",
    "ASSISTANT_COMPOSE_PROJECT": "Executor alias for the restricted COMPOSE_PROJECT_NAME.",
    "ASSISTANT_CONFIG_DIR": "Container-internal prompt configuration path.",
    "ASSISTANT_CONTAINER_ADMIN": "Internal flag identifying the management container.",
    "ASSISTANT_CREDENTIAL_STORE": "Container-internal credential volume path.",
    "ASSISTANT_DEFAULTS_DIR": "Internal defaults path, not an editable setting.",
    "ASSISTANT_EXPECTED_REVISION": "Installer-supplied commit to verify against the running API.",
    "ASSISTANT_INSTALL_DIAGNOSTICS": "One-shot installer flag; raw Docker errors go only to its protected log.",
    "ASSISTANT_REVISION": "Exact source revision baked into the locally built management image.",
    "ASSISTANT_MANAGEMENT_EXECUTOR": "Internal flag identifying the private executor.",
    "ASSISTANT_MANAGEMENT_EXECUTOR_URL": "Private executor network address fixed by Compose.",
    "ASSISTANT_MANAGEMENT_READER_URL": "Private persistent reader address fixed by Compose.",
    "ASSISTANT_MANAGEMENT_URL": "Local client connection override; validated as a loopback address.",
    "ASSISTANT_MANAGEMENT_TOKEN_FILE": "Protected token path supplied to the API client by the launcher.",
    "DOCKER_HOST": "Host Docker context and the private Compose proxy determine Docker connectivity.",
    "EGRESS_POLICY": "The broker injects the approved policy into each ephemeral proxy.",
    "EGRESS_PROXY_PORT": "The broker and proxy use the same fixed internal port.",
    "HOSTNAME": "Container identity supplied by the container runtime.",
    "SANDBOX_DOCKER_SOCKET": "Container-internal Docker socket mount path.",
    "SANDBOX_RUN_IMAGE": "Compose alias for the editable SANDBOX_IMAGE setting.",
    "SANDBOX_SHARED_ROOT": "Container-internal workspace mount path.",
    "SANDBOX_RUN_SHARED_ROOT": "Run-container mount target; the managed deployment fixes it to the workspace path.",
    "SANDBOX_SHARED_VOLUME_NAME": "Unmanaged named-volume mount wiring; the managed installation uses its explicit host workspace mount.",
    "SHARED_FILE_UMASK": "Legacy fallback; managed Compose forwards AGENT_FILESYSTEM_SHARED_FILE_UMASK.",
}
RUNTIME_UNSUPPORTED = {
}
INSTALLER_BUILD_VARIABLES = {
    "ASSISTANT_REVISION": "Installer-owned image identity, not an editable deployment setting.",
}

API_TESTS = "src/agent/tests/test_management_api.py"
TEST_EVIDENCE = {
    "configuration.read": (API_TESTS, "test_v2_configuration_exposes_settings_and_only_redacted_credentials"),
    "configuration.model-settings": (API_TESTS, "test_model_settings_overview_is_complete_revision_bound_and_secret_safe"),
    "configuration.preview": (API_TESTS, "test_v2_configuration_preview_is_revision_checked_and_secret_safe"),
    "configuration.save": (API_TESTS, "test_v2_catalog_settings_preview_save_and_reload"),
    "configuration.check": (API_TESTS, "test_configuration_check_endpoint_reports_saved_problems_without_runtime"),
    "service.list": ("src/agent/tests/test_management_service_coverage.py",
                     "test_service_status_includes_inactive_profiles_and_one_shot_services"),
    "service.logs": (API_TESTS, "test_services_logs_are_shared_redacted_and_bounded"),
    "service.lifecycle": ("src/agent/tests/test_server_control.py",
                          "test_management_api_restarts_both_control_plane_services_safely"),
    "credentials.list": (API_TESTS, "test_v2_catalog_settings_preview_save_and_reload"),
    "credentials.storage": (API_TESTS, "test_v2_catalog_settings_preview_save_and_reload"),
    "credentials.replace": ("src/agent/tests/test_management.py",
                            "test_failed_credential_save_preserves_the_saved_snapshot_and_can_be_retried"),
    "credentials.apply": ("src/agent/tests/test_management.py",
                          "test_named_credential_activation_only_applies_the_requested_pending_credential"),
    "credentials.move": ("src/agent/tests/test_setup.py", "SetupTest::test_move_credentials_back_to_host_and_then_volume_again"),
    "database.reset": ("src/agent/tests/test_factory_reset.py", "ResetOrchestrationTest::test_reset_keeps_management_control_plane_running"),
    "database.recover": ("src/agent/tests/test_setup.py",
                         "SetupTest::test_volume_database_rotation_recovery_keeps_journal_out_of_host_files"),
    "project.delete-preview": ("src/agent/tests/test_workstreams.py", "test_operator_json_preview_and_confirmed_delete_joined_flow"),
    "project.pending-deletions": ("src/agent/tests/test_management.py",
                                  "test_resuming_project_deletion_keeps_writers_stopped_until_completion_is_verified"),
    "project.delete": ("src/agent/tests/test_management.py",
                       "test_project_deletion_only_restores_writers_after_verified_completion"),
    "project.resume": ("src/agent/tests/test_management.py",
                       "test_resuming_project_deletion_keeps_writers_stopped_until_completion_is_verified"),
    "credentials.generate-database-password": (API_TESTS, "test_database_password_generation_does_not_replace_existing_database"),
    "search.maintenance": (API_TESTS, "test_search_maintenance_results_remain_available_without_sensitive_fields"),
    "task.summary": (API_TESTS, "test_management_task_conversation_and_research_reply"),
    "task.list": (API_TESTS, "test_task_deletion_through_management_api"),
    "task.read": (API_TESTS, "test_task_deletion_through_management_api"),
    "task.events": (API_TESTS, "test_task_deletion_through_management_api"),
    "task.cancel": (API_TESTS, "test_task_cancel_retry_and_instruct_through_management_api"),
    "task.review": ("src/agent/tests/test_review_diagnostics_and_approval.py",
                    "EmailApprovalOverrideTest::test_apply_email_review_override_requeues_job"),
    "task.retry": (API_TESTS, "test_task_cancel_retry_and_instruct_through_management_api"),
    "task.instruct": (API_TESTS, "test_task_cancel_retry_and_instruct_through_management_api"),
    "reminder.list": (API_TESTS, "test_task_cancel_retry_and_instruct_through_management_api"),
    "reminder.cancel": (API_TESTS, "test_task_cancel_retry_and_instruct_through_management_api"),
    "task.start": (API_TESTS, "test_management_task_conversation_and_research_reply"),
    "task.reply": (API_TESTS, "test_management_reply_uses_latest_request_channel"),
    "task.messages": (API_TESTS, "test_task_messages_include_sent_mail_once_and_paginate_stably"),
    "task.delete-preview": (API_TESTS, "test_task_deletion_through_management_api"),
    "task.delete": (API_TESTS, "test_task_deletion_through_management_api"),
    **{f"memory.{action}": (API_TESTS, "test_memory_management_through_api")
       for action in ("create", "read", "list", "update", "delete", "approve", "reject", "pin", "unpin", "expire")},
    "research.start": (API_TESTS, "test_management_research_start_is_atomic_and_uses_research_workflow"),
    "research.list": (API_TESTS, "test_management_task_conversation_and_research_reply"),
    "research.read": (API_TESTS, "test_management_task_conversation_and_research_reply"),
    "research.events": (API_TESTS, "test_management_task_conversation_and_research_reply"),
    "research.reply": (API_TESTS, "test_management_task_conversation_and_research_reply"),
    "approval.list": (API_TESTS, "test_approval_review_and_single_use_through_management_api"),
    "approval.decide": (API_TESTS, "test_approval_review_and_single_use_through_management_api"),
    "permissions.host.block": (API_TESTS, "test_management_permission_controls"),
    "permissions.host.unblock": (API_TESTS, "test_management_permission_removals_update_real_grants_and_rules"),
    "permissions.grant.revoke": (API_TESTS, "test_management_permission_removals_update_real_grants_and_rules"),
    "permissions.legacy.remove": (API_TESTS, "test_management_permission_removals_update_real_grants_and_rules"),
    "permissions.hosts": (API_TESTS, "test_management_permission_controls"),
    "permissions.history": (API_TESTS, "test_management_permission_controls"),
    **{f"permissions.{action}": (API_TESTS, "test_script_permission_management_through_api")
       for action in ("scripts", "script.read", "script.block", "script.unblock", "script.revoke")},
    "project.files": (API_TESTS, "test_project_file_reads_are_attached_path_safe_and_binary_safe"),
    "project.file.download": (API_TESTS, "test_project_file_reads_are_attached_path_safe_and_binary_safe"),
    "project.file.upload": (API_TESTS, "test_v2_project_file_routes_and_upload_confirmation"),
    "project.file.attach": ("src/agent/tests/test_workstreams.py", "test_management_project_file_upload_listing_read_and_attach"),
}


# Explicit user-facing requirements, with the existing domain entry point as
# evidence. A contract match is intentionally not called verified coverage.
REQUIREMENTS = (
    ("configuration.read", "src/agent/src/assistant_agent/management.py:Management.load",
     "GET", "/v2/configuration", None, None),
    ("configuration.model-settings", "src/agent/src/assistant_agent/management_model_settings.py:overview",
     "GET", "/v2/configuration/model-settings", None, None),
    ("configuration.preview", "src/agent/src/assistant_agent/management.py:Management.problems",
     "POST", "/v2/configuration/preview", None, None),
    ("configuration.save", "src/agent/src/assistant_agent/management.py:Management.save",
     "POST", "/v2/operations", "operation", "configuration.save"),
    ("configuration.check", "src/agent/src/assistant_agent/config_check.py",
     "GET", "/v2/configuration/check", None, None),
    ("service.list", "src/agent/src/assistant_agent/management.py:Management.services",
     "GET", "/v2/services", None, None),
    ("service.logs", "src/agent/src/assistant_agent/management.py:Management.logs",
     "GET", "/v2/services/{service_id}/logs", None, None),
    ("service.lifecycle", "src/agent/src/assistant_agent/management.py:Management.lifecycle",
     "POST", "/v2/operations", "operation", "service.lifecycle"),
    ("credentials.list", "src/agent/src/assistant_agent/management.py:Management.load",
     "GET", "/v2/credentials", None, None),
    ("credentials.storage", "src/agent/src/assistant_agent/management.py:Management.storage",
     "GET", "/v2/credentials/storage", None, None),
    ("credentials.generate-database-password", "src/agent/src/assistant_agent/management.py:Management.generate_database_password",
     "POST", "/v2/credentials/database-password/generate", None, None),
    ("credentials.replace", "src/agent/src/assistant_agent/management.py:Management.replace_credential",
     "POST", "/v2/operations", "operation", "credential.replace"),
    ("credentials.apply", "src/agent/src/assistant_agent/management.py:Management.apply_credentials",
     "POST", "/v2/operations", "operation", "credential.apply"),
    ("credentials.move", "src/agent/src/assistant_agent/management.py:Management.storage",
     "POST", "/v2/operations", "operation", "credential-storage.move"),
    ("database.reset", "src/agent/src/assistant_agent/management.py:Management.reset",
     "POST", "/v2/operations", "operation", "database.reset"),
    ("database.recover", "src/agent/src/assistant_agent/management.py:Management.recover",
     "POST", "/v2/operations", "operation", "database.recover"),
    ("search.maintenance", "src/agent/src/assistant_agent/management.py:Management.embeddings",
     "POST", "/v2/operations", "operation", "search.maintenance"),
    ("project.delete-preview", "src/agent/src/assistant_agent/management.py:Management.preview_project",
     "GET", "/v2/projects/{identifier}/deletion-preview", None, None),
    ("project.pending-deletions", "src/agent/src/assistant_agent/management.py:Management.pending_deletions",
     "GET", "/v2/projects/pending-deletions", None, None),
    ("project.resume", "src/agent/src/assistant_agent/management.py:Management.resume_deletion",
     "POST", "/v2/operations", "operation", "project.resume"),
    ("task.summary", "src/agent/src/assistant_agent/admin_commands.py:AdminCommands.execute",
     "GET", "/v2/tasks/summary", None, None),
    ("task.list", "src/agent/src/assistant_agent/management_operator.py:COLLECTIONS",
     "GET", "/v2/records/{collection}", "collection", "tasks"),
    ("task.read", "src/agent/src/assistant_agent/management_operator.py:execute",
     "GET", "/v2/records/{collection}/{identifier}", "read_collection", "tasks"),
    ("task.events", "src/agent/src/assistant_agent/management_operator.py:execute",
     "GET", "/v2/records/{collection}/{identifier}/children", "children_collection", "tasks"),
    ("task.cancel", "src/agent/src/assistant_agent/admin_commands.py:AdminCommands.execute",
     "POST", "/v2/operations", "record_action", "cancel"),
    ("task.review", "src/agent/src/assistant_agent/admin_commands.py:AdminCommands.execute",
     "POST", "/v2/operations", "record_action", "review"),
    ("task.retry", "src/agent/src/assistant_agent/admin_commands.py:AdminCommands.execute",
     "POST", "/v2/operations", "record_action", "retry"),
    ("task.instruct", "src/agent/src/assistant_agent/admin_commands.py:AdminCommands.execute",
     "POST", "/v2/operations", "record_action", "instruct"),
    ("task.start", "src/agent/src/assistant_agent/requests.py:RequestService.receive_request",
     "POST", "/v2/operations", "operation", "task.start"),
    ("task.reply", "src/agent/src/assistant_agent/requests.py:RequestService.receive_request",
     "POST", "/v2/operations", "operation", "task.reply"),
    ("task.messages", "src/agent/src/assistant_agent/requests.py:RequestService.receive_request",
     "GET", "/v2/tasks/{identifier}/messages", None, None),
    ("task.delete-preview", "src/agent/src/assistant_agent/task_deletion.py:TaskDeletionService.preview",
     "GET", "/v2/tasks/{identifier}/deletion-preview", None, None),
    ("task.delete", "src/database/schema.sql:jobs",
     "POST", "/v2/operations", "operation", "task.delete"),
    ("memory.expire", "src/agent/src/assistant_agent/admin_commands.py:AdminCommands.memory",
     "POST", "/v2/operations", "record_action", "expire-memory"),
    *tuple(
        (f"memory.{name}", "src/agent/src/assistant_agent/admin_commands.py:AdminCommands.memory",
         "POST", "/v2/operations", "record_action", action)
        for name, action in (
            ("create", "create-memory"), ("update", "update-memory"), ("delete", "delete-memory"),
            ("approve", "approve-memory"), ("reject", "reject-memory"), ("pin", "pin-memory"),
            ("unpin", "unpin-memory"),
        )
    ),
    ("memory.list", "src/agent/src/assistant_agent/admin_commands.py:AdminCommands.memory",
     "GET", "/v2/records/{collection}", "collection", "memories"),
    ("memory.read", "src/agent/src/assistant_agent/admin_commands.py:AdminCommands.memory",
     "GET", "/v2/records/{collection}/{identifier}", "read_collection", "memories"),
    ("reminder.list", "src/agent/src/assistant_agent/admin_commands.py:AdminCommands.execute",
     "GET", "/v2/records/{collection}", "collection", "reminders"),
    ("reminder.cancel", "src/agent/src/assistant_agent/admin_commands.py:AdminCommands.execute",
     "POST", "/v2/operations", "record_action", "cancel-reminder"),
    ("permissions.scripts", "src/agent/src/assistant_agent/script_permissions.py:management",
     "GET", "/v2/records/{collection}", "collection", "scripts"),
    ("permissions.script.read", "src/agent/src/assistant_agent/script_permissions.py:management",
     "GET", "/v2/records/{collection}/{identifier}", "read_collection", "scripts"),
    ("permissions.hosts", "src/agent/src/assistant_agent/script_permissions.py:management",
     "GET", "/v2/permissions/{view}", None, None),
    ("permissions.history", "src/agent/src/assistant_agent/script_permissions.py:management",
     "GET", "/v2/permissions/{view}", None, None),
    *tuple(
        (f"permissions.script.{name}", "src/agent/src/assistant_agent/script_permissions.py:management",
         "POST", "/v2/operations", "record_action", action)
        for name, action in (("block", "block-script"), ("unblock", "unblock-script"), ("revoke", "revoke-script"))
    ),
    ("research.list", "src/agent/src/assistant_agent/deep_research.py",
     "GET", "/v2/records/{collection}", "collection", "research"),
    ("research.read", "src/agent/src/assistant_agent/deep_research.py",
     "GET", "/v2/records/{collection}/{identifier}", "read_collection", "research"),
    ("research.events", "src/agent/src/assistant_agent/deep_research.py",
     "GET", "/v2/records/{collection}/{identifier}/children", "children_collection", "research"),
    ("research.start", "src/agent/src/assistant_agent/deep_research.py",
     "POST", "/v2/operations", "operation", "research.start"),
    ("research.reply", "src/agent/src/assistant_agent/requests.py:RequestService.resume_waiting_research",
     "POST", "/v2/operations", "operation", "task.reply"),
    ("approval.list", "src/agent/src/assistant_agent/email_approvals.py:EmailApprovals",
     "GET", "/v2/approvals", None, None),
    ("approval.decide", "src/agent/src/assistant_agent/email_approvals.py:EmailApprovals",
     "POST", "/v2/operations", "operation", "approval.decide"),
    ("project.delete", "src/agent/src/assistant_agent/workstreams.py:WorkstreamStore.execute_delete",
     "POST", "/v2/operations", "operation", "project.delete"),
    ("project.files", "src/agent/src/assistant_agent/workstream_files.py:WorkstreamFileTracker",
     "GET", "/v2/projects/{identifier}/files", None, None),
    ("project.file.download", "src/agent/src/assistant_agent/workstream_files.py:WorkstreamFileTracker",
     "GET", "/v2/projects/{identifier}/files/{file_id}/content", None, None),
    ("project.file.upload", "src/agent/src/assistant_agent/workstream_files.py:WorkstreamFileTracker",
     "POST", "/v2/operations", "operation", "file.upload"),
    ("project.file.attach", "src/agent/src/assistant_agent/workstreams.py:WorkstreamStore.attach",
     "POST", "/v2/operations", "operation", "file.attach"),
    ("project.list", "src/agent/src/assistant_agent/workstreams.py",
     "GET", "/v2/records/{collection}", "collection", "projects"),
    ("project.read", "src/agent/src/assistant_agent/workstreams.py",
     "GET", "/v2/records/{collection}/{identifier}", "read_collection", "projects"),
    ("permissions.host.block", "src/agent/src/assistant_agent/script_permissions.py:management",
     "POST", "/v2/operations", "permission_action", "block-host"),
    ("permissions.host.unblock", "src/agent/src/assistant_agent/script_permissions.py:management",
     "POST", "/v2/operations", "permission_action", "unblock-host"),
    ("permissions.grant.revoke", "src/agent/src/assistant_agent/script_permissions.py:management",
     "POST", "/v2/operations", "permission_action", "revoke-grant"),
    ("permissions.legacy.remove", "src/agent/src/assistant_agent/script_permissions.py:management",
     "POST", "/v2/operations", "permission_action", "remove-legacy"),
)

ADMIN_REQUIREMENTS = {
    "/admin status": ("task.summary",),
    "/admin jobs [--cursor ID] [--limit N]": ("task.list",),
    "/admin jobs --recent [--limit N]": ("task.list",),
    "/admin logs JOB_ID [--cursor SEQUENCE] [--limit N]": ("task.events",),
    "/admin review JOB_ID": ("task.review",),
    "/admin cancel JOB_ID": ("task.cancel",),
    "/admin requeue JOB_ID": ("task.retry",),
    "/admin instruct JOB_ID (instruction in message body; finished jobs are requeued)": ("task.instruct",),
    "/admin memory list|search|suggestions [--query TEXT] [--limit N]": ("memory.list",),
    "/admin memory show|update|delete|approve|reject|pin|unpin|expire ID": (
        "memory.read", "memory.update", "memory.delete", "memory.approve", "memory.reject",
        "memory.pin", "memory.unpin", "memory.expire",
    ),
    "/admin memory create (memory text in message body)": ("memory.create",),
    "/admin reminders [--limit N]": ("reminder.list",),
    "/admin reminder-cancel ID": ("reminder.cancel",),
    "/admin permissions scripts|hosts|history": ("permissions.scripts", "permissions.hosts", "permissions.history"),
    "/admin permissions show VERSION_ID": ("permissions.script.read",),
    "/admin permissions block-script|unblock-script|revoke-script SCRIPT_ID": (
        "permissions.script.block", "permissions.script.unblock", "permissions.script.revoke",
    ),
    "/admin permissions block-host|unblock-host HOST": ("permissions.host.block", "permissions.host.unblock"),
    "/admin permissions revoke-grant GRANT_ID": ("permissions.grant.revoke",),
    "/admin permissions remove-legacy RULE_ID": ("permissions.legacy.remove",),
}

TUI_REQUIREMENTS = {
    "apply_credentials": ("credentials.apply",),
    "delete_project": ("project.delete",),
    "draft": ("configuration.preview",),
    "embeddings": ("search.maintenance",),
    "generate_database_password": ("credentials.generate-database-password",),
    "instruction_text": ("configuration.read",),
    "model_settings": ("configuration.model-settings",),
    "lifecycle": ("service.lifecycle",),
    "load": ("configuration.read",),
    "logs": ("service.logs",),
    "mutate": ("task.cancel", "memory.create", "reminder.cancel", "permissions.script.block"),
    "pending_deletions": ("project.pending-deletions",),
    "prepare_management": (),
    "preview_project": ("project.delete-preview",),
    "problems": ("configuration.preview",),
    "recover": ("database.recover",),
    "redact": (),
    "replace_credential": ("credentials.replace",),
    "request": ("task.list", "memory.list", "reminder.list", "permissions.scripts", "research.list"),
    "reset": ("database.reset",),
    "resume_deletion": ("project.resume",),
    "save": ("configuration.save",),
    "services": ("service.list",),
    "stage": ("configuration.preview",),
    "storage": ("credentials.storage", "credentials.move"),
    "value": ("configuration.read",),
}

MODEL_TOOL_REQUIREMENTS = {
    "deep_research_request": ("research.start",),
    "deep_research_status": ("research.read",),
    "job_search": ("task.list",),
    "job_list": ("task.list",),
    "job_read": ("task.read",),
}
MODEL_TOOL_REQUIREMENTS.update({
    method: ("domain." + action,)
    for action, method in {**READ_ACTIONS, **WRITE_ACTIONS}.items()
})
MODEL_TOOL_REQUIREMENTS.update({
    "file_list": ("domain.workspace.list",), "file_read": ("domain.workspace.read",),
    "file_write": ("domain.workspace.write",), "file_append": ("domain.workspace.write",),
    "workstream_list": ("project.list",), "workstream_read": ("project.read",),
    "workstream_attach": ("domain.projects.attach",),
})
AGENT_MEDIATED_TOOLS = {
    "calendar_create_event", "calendar_delete_event", "calendar_list_busy", "calendar_list_calendars",
    "calendar_list_events", "calendar_sync", "calendar_update_event", "command_execute",
    "contact_create", "contact_delete", "contact_read", "contact_search", "contact_update",
    "context_search", "development_session_request", "email_read", "email_search", "email_send",
    "file_append", "file_convert", "file_copy", "file_delete", "file_list", "file_move", "file_read",
    "file_search", "file_semantic_search", "file_write", "get_tool_specs", "help_search", "help_topic",
    "llm_service", "memory_forget", "memory_remember", "memory_search", "memory_update",
    "network_access_request", "reminder_cancel", "reminder_create", "reminder_list", "reminder_read", "reminder_update",
    "request_input", "request_limit_increase", "runtime_capabilities", "script_approval_request",
    "skill_list", "skill_search", "skill_load", "skill_save",
    "script_development_review", "telegram_send", "text_to_speech", "web_fetch", "web_search", "workstream_attach",
    "workstream_list", "workstream_read",
}
WORKER_INTERNAL_TOOLS = {
    "research_complete", "research_failed", "research_request_human_input", "task_complete", "task_failed",
}

# Public entry points are candidates for review, not automatically required API
# features: internal helpers and agent tools are not equivalent to client APIs.
INVENTORY_FILES = (
    "src/agent/src/assistant_agent/admin_commands.py",
    "src/agent/src/assistant_agent/management.py",
    "src/agent/src/assistant_agent/management_operator.py",
    "src/agent/src/assistant_agent/requests.py",
    "src/agent/src/assistant_agent/request_router.py",
    "src/agent/src/assistant_agent/deep_research.py",
    "src/agent/src/assistant_agent/workstreams.py",
    "src/agent/src/assistant_agent/workstream_operator.py",
    "src/agent/src/assistant_agent/email_approvals.py",
    "src/agent/src/assistant_agent/setup.py",
    "src/agent/src/assistant_agent/setup_tools.py",
    "src/agent/src/assistant_agent/server_control.py",
    "src/agent/src/assistant_agent/script_permissions.py",
    "src/agent/src/assistant_agent/tools.py",
)


def literals(model, field):
    return set(get_args(model.model_fields[field].annotation))


def operation_names():
    union = get_args(ManagementOperationRequest)[0]
    return {
        value
        for model in get_args(union)
        for value in literals(model, "operation")
    }


def executor_read_handlers(root=ROOT):
    source = root / "src/agent/src/assistant_agent/management_executor.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    handlers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Handler":
            continue
        for member in node.body:
            if not isinstance(member, ast.FunctionDef) or member.name != "read":
                continue
            for branch in ast.walk(member):
                if not isinstance(branch, ast.If) or not isinstance(branch.test, ast.Compare):
                    continue
                condition = branch.test
                if (isinstance(condition.left, ast.Name) and condition.left.id == "name"
                        and len(condition.ops) == 1 and isinstance(condition.ops[0], ast.Eq)
                        and len(condition.comparators) == 1
                        and isinstance(condition.comparators[0], ast.Constant)
                        and isinstance(condition.comparators[0].value, str)
                        and any(isinstance(item, ast.Return) for item in branch.body)):
                    handlers.add(condition.comparators[0].value)
    reader = root / "src/agent/src/assistant_agent/management_reader.py"
    if reader.is_file():
        for node in ast.walk(ast.parse(reader.read_text(encoding="utf-8"), filename=str(reader))):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                    and any(isinstance(target, ast.Name) and target.id == "READ_KINDS" for target in node.targets)):
                handlers.update(key.value for key in node.value.keys if isinstance(key, ast.Constant)
                                and isinstance(key.value, str))
    return handlers


def executor_reads():
    union = get_args(ManagementReadRequest)[0]
    names = {name for model in get_args(union) for name in literals(model, "request")}
    handlers = executor_read_handlers()
    return [{"name": name, "status": "handler_present" if name in handlers else "missing"}
            for name in sorted(names)]


def routes():
    with tempfile.TemporaryDirectory(prefix="assistant-management-audit-") as root:
        app = create_app(root, "audit-token")
        found = {
            (method, route.path)
            for route in app.routes
            for method in getattr(route, "methods", set())
        }
        app.state.management_operations.executor.shutdown(wait=True)
    return found


def load_compose(text):
    tree = yaml.compose(text)
    visited = set()

    def check(node):
        if id(node) in visited:
            return
        visited.add(id(node))
        if isinstance(node, yaml.MappingNode):
            keys = set()
            for key, value in node.value:
                if isinstance(key, yaml.ScalarNode) and key.tag != "tag:yaml.org,2002:merge":
                    if key.value in keys:
                        raise ValueError(f"Duplicate Compose key {key.value!r} at line {key.start_mark.line + 1}")
                    keys.add(key.value)
                check(value)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                check(item)

    if tree is not None:
        check(tree)
    return yaml.safe_load(text)


def inventory(root=ROOT):
    result = []
    for path in runtime_sources(root):
        relative = str(path.relative_to(root))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                result.append({"symbol": node.name, "source": f"{relative}:{node.lineno}"})
            elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and not member.name.startswith("_"):
                        result.append({"symbol": f"{node.name}.{member.name}",
                                       "source": f"{relative}:{member.lineno}"})
    return result


def runtime_sources(root=ROOT):
    return sorted(path for path in (root / "src").rglob("*.py")
                  if not path.name.startswith("test_")
                  and not {"tests", "scripts", "__pycache__", "node_modules"} & set(path.relative_to(root).parts))


def admin_commands(requirements=None):
    """Discover documented command forms independently of the API mapping."""
    result = []
    for line in HELP.splitlines():
        for match in re.finditer(r"/admin .*?(?=/admin |$)", line):
            command = match.group(0).strip().rstrip("|").strip()
            mapped = ADMIN_REQUIREMENTS.get(command)
            status = "unclassified" if mapped is None or requirements is None else (
                "contract_present" if all(requirements.get(name) == "contract_present" for name in mapped)
                else "missing"
            )
            result.append({"command": command, "status": status, "requirements": list(mapped or ())})
    return result


def tui_backend_methods(requirements, root=ROOT):
    source = "src/agent/src/assistant_agent/management_tui.py"
    tree = ast.parse((root / source).read_text(encoding="utf-8"), filename=source)
    found = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name) and node.value.value.id == "self"
        and node.value.attr == "backend" and callable(getattr(Management, node.attr, None))
    }
    rows = []
    for name in sorted(found):
        mapped = TUI_REQUIREMENTS.get(name)
        state = "unclassified" if mapped is None else "client_local" if not mapped else (
            "contract_present" if all(requirements.get(key) == "contract_present" for key in mapped) else "missing"
        )
        rows.append({"method": name, "requirements": list(mapped or ()), "status": state})
    return rows


def tool_definitions(root=ROOT, requirements=None):
    """Find literal model tool declarations; dynamically generated tools need review."""
    result = []
    for path in runtime_sources(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            fields = {
                key.value: value for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            name = fields.get("name")
            if (isinstance(name, ast.Constant) and isinstance(name.value, str)
                    and "description" in fields and "parameters" in fields):
                mapped = MODEL_TOOL_REQUIREMENTS.get(name.value)
                if mapped is not None and requirements is not None:
                    state = ("contract_present" if all(requirements.get(key) == "contract_present" for key in mapped)
                             else "missing")
                elif name.value in AGENT_MEDIATED_TOOLS:
                    state = "agent_mediated"
                elif name.value in WORKER_INTERNAL_TOOLS:
                    state = "worker_internal"
                else:
                    state = "unclassified"
                result.append({
                    "name": name.value, "source": f"{path.relative_to(root)}:{node.lineno}",
                    "status": state, "requirements": list(mapped or ()),
                })
    return sorted(result, key=lambda row: (row["name"], row["source"]))


def dynamic_calendar_settings():
    for provider in ("GOOGLE", "OUTLOOK", "ZOHO"):
        fields = ["CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN", "CALENDAR_ID"]
        if provider == "OUTLOOK":
            fields.append("TENANT_ID")
        elif provider == "ZOHO":
            fields.append("API_BASE")
        for number in range(2, 10):
            for field in fields:
                yield {
                    "env": f"AGENT_CALENDAR_{provider}_{number}_{field}",
                    "path": None,
                    "secret": field in {"CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN"},
                }


def compose_variables(text):
    return sorted(set(re.findall(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)", text)))


def runtime_environment(root=ROOT):
    found = {}
    for relative in ("src/agent/src/assistant_agent", "src/sandbox", "src/transcription"):
        for path in sorted((root / relative).rglob("*.py")):
            if path.name.startswith("test_") or "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                    target = node.func
                    is_getenv = (isinstance(target, ast.Attribute) and target.attr == "getenv"
                                 and isinstance(target.value, ast.Name) and target.value.id == "os")
                    is_env_get = (isinstance(target, ast.Attribute) and target.attr == "get"
                                  and isinstance(target.value, ast.Attribute) and target.value.attr == "environ"
                                  and isinstance(target.value.value, ast.Name) and target.value.value.id == "os")
                    is_secret_read = isinstance(target, ast.Name) and target.id == "read_secret"
                    if is_getenv or is_env_get or is_secret_read:
                        name = node.args[0].value
                elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                      and node.value.attr == "environ" and isinstance(node.value.value, ast.Name)
                      and node.value.value.id == "os" and isinstance(node.slice, ast.Constant)):
                    name = node.slice.value
                if isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                    found.setdefault(name, []).append(f"{path.relative_to(root)}:{node.lineno}")
    return found


def test_evidence(references=None):
    references = TEST_EVIDENCE if references is None else references
    found = {}
    for path in {item[0] for item in references.values()}:
        source = ROOT / path
        if not source.is_file():
            found[path] = set()
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=path)
        found[path] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[path].add(node.name)
            elif isinstance(node, ast.ClassDef):
                found[path].update(
                    f"{node.name}::{member.name}" for member in node.body
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
    return {
        feature: {"test": f"{path}::{name}", "present": name in found[path]}
        for feature, (path, name) in references.items()
    }


def run_test_evidence(evidence):
    selectors = sorted({item["test"] for item in evidence.values() if item["present"]})
    if not selectors:
        return {"exit_code": None, "error": "No test references are available.", "tests": {}}
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(ROOT / "src/agent/src") + (os.pathsep + existing if existing else "")
    with tempfile.TemporaryDirectory(prefix="assistant-management-tests-") as directory:
        report_path = Path(directory) / "results.xml"
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--tb=short", f"--junitxml={report_path}", *selectors],
                cwd=ROOT, env=environment, stdout=sys.stderr, stderr=subprocess.STDOUT, check=False,
            )
        except OSError as error:
            return {"exit_code": None, "error": f"Could not start pytest: {error}", "tests": {}}
        try:
            cases = ElementTree.parse(report_path).getroot().iter("testcase")
            outcomes = []
            for case in cases:
                status = ("failed" if case.find("failure") is not None or case.find("error") is not None
                          else "skipped" if case.find("skipped") is not None else "passed")
                outcomes.append((case.get("classname", ""), case.get("name", "").split("[", 1)[0], status))
        except (OSError, ElementTree.ParseError) as error:
            return {"exit_code": result.returncode, "error": f"Could not read pytest results: {error}", "tests": {}}
    tests = {}
    for selector in selectors:
        path, *parts = selector.split("::")
        name = parts[-1]
        classname = Path(path).with_suffix("").as_posix().replace("/", ".")
        if len(parts) > 1:
            classname += "." + ".".join(parts[:-1])
        statuses = [status for case_class, case_name, status in outcomes
                    if case_class == classname and case_name == name]
        tests[selector] = ("failed" if "failed" in statuses else "skipped" if "skipped" in statuses
                           else "passed" if statuses else "not_collected")
    return {"exit_code": result.returncode, "error": None, "tests": tests}


def audit():
    route_set = routes()
    operations = operation_names()
    collections = literals(RecordListRequest, "collection") & (set(COLLECTIONS) | {"scripts"})
    read_collections = literals(RecordReadRequest, "collection") & (set(COLLECTIONS) | {"scripts"})
    children_collections = literals(RecordChildrenRequest, "collection") & {"tasks", "projects", "research"}
    actions = literals(RecordMutationOperation, "action") & set(MUTATIONS)
    permission_actions = literals(PermissionControlOperation, "action") if "permission.control" in operations else set()
    lifecycle_actions = literals(ServiceLifecycleOperation, "action")
    setting_fields = set(ManagementService._setting_specs())
    settings = []
    for item in [*settings_schema.catalog(), *dynamic_calendar_settings()]:
        present = (
            item["env"] in setting_fields
            and ("GET", "/v2/configuration") in route_set
            and ("POST", "/v2/configuration/preview") in route_set
            and ("POST", "/v2/operations") in route_set
            and "configuration.save" in operations
        )
        settings.append({
            "name": item["env"], "path": item["path"], "secret": item["secret"],
            "status": ("restricted" if item["env"] in DERIVED_SETTINGS else "contract_present")
            if present else "missing",
            "restriction": DERIVED_SETTINGS.get(item["env"]),
            "evidence": "GET /v2/configuration; POST /v2/configuration/preview; configuration.save"
            if present else None,
        })

    compose_text = (ROOT / "src/docker-compose.yml").read_text(encoding="utf-8")
    compose = load_compose(compose_text)
    deployment_variables = [
        {
            "name": name,
            "status": "restricted" if name in INSTALLER_BUILD_VARIABLES else (
                ("restricted" if name in DERIVED_SETTINGS else "contract_present")
                if name in setting_fields else "missing"
            ),
            "restriction": DERIVED_SETTINGS.get(name) or INSTALLER_BUILD_VARIABLES.get(name),
        }
        for name in compose_variables(compose_text)
    ]
    runtime_variables = []
    for name, sources in sorted(runtime_environment().items()):
        pointer = name.endswith("_FILE") and is_secret_name(name.removesuffix("_FILE"))
        status = ("restricted" if name in DERIVED_SETTINGS else "contract_present") if name in setting_fields else (
            "credential_pointer" if pointer else "internal" if name in RUNTIME_INTERNAL else
            "unsupported_deployment" if name in RUNTIME_UNSUPPORTED else "unclassified"
        )
        runtime_variables.append({"name": name, "status": status, "sources": sources,
                                  "reason": RUNTIME_INTERNAL.get(name) or RUNTIME_UNSUPPORTED.get(name)})
    service_rows = []
    for name, definition in sorted(compose["services"].items()):
        # Check the real control-plane guard without invoking Docker.
        backend = Management(ROOT, lambda *args: False)
        backend.installation = Mock(state={"setup_complete": True})
        backend.installation.assert_current = Mock()
        backend.installation.compose.return_value = ""
        backend.access = lambda: None
        backend._service_catalog = lambda: compose["services"]
        backend.services = lambda: []
        try:
            if name in {"management", "management-docker"}:
                with patch.dict(os.environ, {"ASSISTANT_MANAGEMENT_EXECUTOR": "1"}):
                    backend.lifecycle("restart", name, preserve_management=True)
        except SetupError:
            restart = "blocked"
        else:
            restart = "unverified"
        service_rows.append({
            "name": name, "profiles": definition.get("profiles", []),
            "healthcheck_defined": bool(definition.get("healthcheck")),
            "one_shot": definition.get("restart", "no") == "no",
            "status_contract": ("GET", "/v2/services") in route_set,
            "logs_contract": ("GET", "/v2/services/{service_id}/logs") in route_set,
            "restart_contract": "service.lifecycle" in operations and "restart" in lifecycle_actions,
            "restart": restart,
        })

    requirements = []
    evidence = test_evidence()
    for name, source, method, path, selector, value in REQUIREMENTS:
        present = (method, path) in route_set
        if selector:
            present = present and value in {
                "operation": operations, "collection": collections, "read_collection": read_collections,
                "children_collection": children_collections, "record_action": actions,
                "permission_action": permission_actions,
            }[selector]
        requirements.append({
            "name": name, "source": source, "api": f"{method} {path}",
            "selector": value, "status": "contract_present" if present else "missing",
            "test_evidence": evidence.get(name),
        })
    for row in requirements:
        if row["name"] in {"project.list", "project.read"}:
            row["test_evidence"] = test_evidence({row["name"]: (
                "src/agent/tests/test_management_domains.py",
                "test_projects_have_discoverable_strict_schemas_and_explicit_lifecycle",
            )})[row["name"]]
    client_evidence = {
        "client.register": ("test_client_channels.py", "test_polling_updates_availability_and_acknowledges_only_offered_owned_messages"),
        "client.unregister": ("test_client_channels.py", "test_missing_fallback_keeps_reply_pollable_and_uncertain_delivery_is_not_retried"),
        "client.poll": ("test_client_channels.py", "test_polling_updates_availability_and_acknowledges_only_offered_owned_messages"),
        "client.acknowledge": ("test_client_channels.py", "test_polling_updates_availability_and_acknowledges_only_offered_owned_messages"),
        "client.fallback": ("test_client_channels.py", "test_offline_or_unacknowledged_reply_falls_back_once"),
        "client.approvals": ("test_management_approvals.py", "test_client_only_approval_is_delivered_without_email_or_otp_and_consumed_once"),
        "client.notifications": ("test_client_channels.py", "test_non_task_notice_falls_back_and_client_failure_notification_is_not_silently_skipped"),
        "client.history": ("test_client_channels.py", "test_history_survives_acknowledgment_and_uncertain_delivery_can_be_resolved"),
        "client.delivery-resolve": ("test_management_api.py", "test_v2_registered_client_chat_and_domain_management_over_http"),
        "client.reconnect": ("test_client_channels.py", "test_registration_can_reuse_a_stable_client_id"),
        "task.workstream-attachments": ("test_client_channels.py", "test_task_start_selects_workstream_and_only_its_attached_files"),
    }
    client_routes = {
        "client.register": ("POST", "/v2/operations", "client.register"),
        "client.unregister": ("POST", "/v2/operations", "client.unregister"),
        "client.poll": ("GET", "/v2/clients/{client_id}/messages", None),
        "client.acknowledge": ("POST", "/v2/clients/{client_id}/acknowledgments", None),
        "client.fallback": ("GET", "/v2/clients/{client_id}/messages", None),
        "client.approvals": ("GET", "/v2/approvals", None),
        "client.notifications": ("GET", "/v2/clients/{client_id}/messages", None),
        "client.history": ("GET", "/v2/clients/{client_id}/history", None),
        "client.delivery-resolve": ("POST", "/v2/operations", "client.delivery-resolve"),
        "client.reconnect": ("POST", "/v2/operations", "client.register"),
        "task.workstream-attachments": ("POST", "/v2/operations", "task.start"),
    }
    for name, (method, path, operation) in client_routes.items():
        file, test = client_evidence[name]
        test_path = "src/agent/tests/" + file
        requirements.append({
            "name": name, "source": "registered polling client channel", "api": f"{method} {path}",
            "selector": operation, "status": "contract_present" if (method, path) in route_set and
            (operation is None or operation in operations) else "missing",
            "test_evidence": test_evidence({name: (test_path, test)})[name],
        })
    allowed_reads, allowed_writes = set(get_args(ReadAction)), set(get_args(WriteAction))
    domain_rows = domain_catalog()["actions"]
    for row in domain_rows:
        action = row["action"]
        access = row["access"]
        required_route = ("POST", "/v2/query") if access == "read" else ("POST", "/v2/operations")
        present = required_route in route_set and action in (allowed_reads if access == "read" else allowed_writes)
        if access == "write":
            present = present and "domain.mutate" in operations
        test = None
        test_path = "src/agent/tests/test_management_domains.py"
        if action in {"contacts.search", "contacts.read", "contacts.create", "contacts.update", "contacts.delete"}:
            test = "test_contact_management_requires_current_versions_and_rejects_unknown_arguments"
        elif action in {"workspace.list", "workspace.read", "workspace.write", "workspace.mkdir"}:
            test = "test_workspace_binary_chunks_preserve_versions_and_reject_symlinks"
        elif action in {"projects.create", "projects.rename", "projects.archive", "projects.restore"}:
            test = "test_projects_have_discoverable_strict_schemas_and_explicit_lifecycle"
        elif action in {"email.search", "email.read"}:
            test = "test_stored_email_remains_readable_with_intake_disabled"
        elif action in {"reminders.list", "reminders.create", "reminders.update", "reminders.cancel"}:
            test = "test_reminders_keep_client_routes_and_prevent_archiving_active_schedules"
        elif action in {"memory.search", "memory.remember", "memory.update", "memory.forget"}:
            test = "test_memory_management_uses_admin_authority_without_fabricating_a_task"
        elif action in {"calendar.calendars", "calendar.events", "calendar.busy", "calendar.sync",
                        "calendar.create", "calendar.update", "calendar.delete"}:
            test = "test_calendar_management_lifecycle_and_reads_use_provider_and_version_checks"
        elif action == "zoom.reconcile":
            test = "test_zoom_reconciliation_requires_matching_provider_state_and_records_identity"
        elif action in {"zoom.list", "zoom.read", "zoom.create", "zoom.update", "zoom.delete"}:
            test = "test_zoom_management_versions_and_provider_calls_use_shared_domain_logic"
        elif action in {"workspace.move", "workspace.copy", "workspace.delete", "projects.attach"}:
            test = "test_workspace_move_copy_delete_and_project_attachment_preserve_tracking"
        elif action == "workspace.convert":
            test = "test_workspace_conversion_publishes_and_tracks_output"
        elif action in {"workspace.search", "workspace.semantic-search", "context.search", "capabilities.read"}:
            test = "test_management_searches_stored_context_and_workspace_without_network"
        elif action in {"dropbox.list", "dropbox.delete"}:
            test = "test_dropbox_browsing_and_revision_guard_do_not_require_a_task"
        elif action in {"dropbox.import", "dropbox.export"}:
            test = "test_dropbox_management_transfers_scan_publish_and_record_task_provenance"
        elif action in {"email.send", "telegram.send"}:
            test = "test_management_email_and_telegram_sends_persist_delivery_and_distinguish_operations"
        elif action == "scripts.execute":
            test = "test_management_script_execution_preserves_sandbox_and_approval_boundary"
        elif action.startswith("skills."):
            test_path = "src/agent/tests/test_management_api.py"
            test = "test_v2_registered_client_chat_and_domain_management_over_http"
        requirements.append({
            "name": "domain." + action, "source": "explicit user-facing domain action",
            "api": " ".join(required_route), "selector": action,
            "status": "contract_present" if present else "missing",
            "test_evidence": test_evidence({action: (test_path, test)})[action] if test else None,
        })
    known_actions = {row["action"] for row in domain_rows}
    unclassified_actions = sorted((allowed_reads - {"catalog"} | allowed_writes) - known_actions)
    return {
        "scope": "Every catalog setting, deployment/runtime variables, services, user-facing domain actions, and all runtime Python entry points",
        "limitations": [
            "Contract presence is not verified end-to-end behavior.",
            "Test references show where selected scenarios are exercised; they do not prove the test ran or cover all behavior.",
            "Internal functions and tables are inventory candidates, not required public API resources.",
            "Admin command mappings represent contract presence, not equivalent pagination or verified behavior.",
            "Agent-mediated tools are not deterministic direct API operations; dynamic tools are not inventoried.",
            "Unclassified runtime variables, dynamic settings families, integrations and security behavior require separate review.",
            "Service healthchecks, profiles, restart safety and operation interruption require runtime tests.",
            "Missing selectors indicate that the mapped contract was not found, not that no alternative could exist.",
            "Cross-channel task routing and delivery require end-to-end testing against live integrations.",
        ],
        "settings": settings, "deployment_variables": deployment_variables,
        "runtime_variables": runtime_variables,
        "services": service_rows, "requirements": requirements,
        "entry_points": inventory(),
        "source_files": [str(path.relative_to(ROOT)) for path in runtime_sources()],
        "domain_actions": domain_rows, "unclassified_domain_actions": unclassified_actions,
        "executor_reads": executor_reads(),
        "admin_commands": admin_commands({item["name"]: item["status"] for item in requirements}),
        "tui_backend_methods": tui_backend_methods({item["name"]: item["status"] for item in requirements}),
        "model_tools": tool_definitions(requirements={item["name"]: item["status"] for item in requirements}),
        "routes": [{"method": method, "path": path} for method, path in sorted(route_set)
                   if path.startswith("/v2/")],
        "operations": sorted(operations),
    }


def coverage_failures(report, *, require_behavior=False):
    failures = []
    for section in ("settings", "deployment_variables", "runtime_variables", "requirements",
                    "model_tools", "admin_commands", "tui_backend_methods", "executor_reads"):
        for row in report[section]:
            if row["status"] in {"missing", "unclassified", "unsupported_deployment"}:
                failures.append(f"{section}: {row.get('name', row.get('command', row.get('method')))} ({row['status']})")
    failures.extend("unclassified domain action: " + action for action in report["unclassified_domain_actions"])
    if require_behavior:
        run = report.get("test_run") or {}
        if run.get("error") or run.get("exit_code") != 0:
            failures.append("Referenced behavioral test run did not pass")
        for row in report["requirements"]:
            evidence = row["test_evidence"]
            if not evidence or not evidence["present"]:
                failures.append("No behavioral evidence: " + row["name"])
            elif run.get("tests", {}).get(evidence["test"]) != "passed":
                failures.append("Behavior not verified: " + row["name"])
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit full machine-readable inventory")
    parser.add_argument("--run-tests", action="store_true", help="run referenced tests and report their outcomes")
    args = parser.parse_args()
    report = audit()
    if args.run_tests:
        report["test_run"] = run_test_evidence({
            row["name"]: row["test_evidence"] for row in report["requirements"] if row["test_evidence"]
        })
    report["coverage_failures"] = coverage_failures(report, require_behavior=args.run_tests)
    report["verification"] = "referenced_behavior" if args.run_tests else "static_contract_only"
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        settings = Counter(item["status"] for item in report["settings"])
        features = Counter(item["status"] for item in report["requirements"])
        print(f"Settings: {settings['contract_present']} contract-present, "
              f"{settings['restricted']} restricted, {settings['missing']} missing")
        deployment = Counter(item["status"] for item in report["deployment_variables"])
        print(f"Compose variables: {deployment['contract_present']} contract-present, "
              f"{deployment['restricted']} restricted, {deployment['missing']} missing")
        runtime = Counter(item["status"] for item in report["runtime_variables"])
        print(f"Runtime variables: {runtime['contract_present']} contract-present, {runtime['restricted']} restricted, "
              f"{runtime['credential_pointer']} credential pointers, {runtime['internal']} internal, "
              f"{runtime['unsupported_deployment']} unsupported deployments, {runtime['unclassified']} unclassified")
        print(f"Features: {features['contract_present']} contract-present, {features['missing']} missing")
        executor_status = Counter(item["status"] for item in report["executor_reads"])
        print(f"Private executor reads: {executor_status['handler_present']} handlers present, "
              f"{executor_status['missing']} missing; dispatch is not runtime verification")
        print(f"Feature test references: {sum(item['test_evidence'] is not None and item['test_evidence']['present'] for item in report['requirements'])}"
              f" of {len(report['requirements'])}; presence is not execution or complete behavioral coverage")
        if args.run_tests:
            test_run = report["test_run"]
            status = Counter(test_run["tests"].values())
            print(f"Referenced tests: {status['passed']} passed, {status['failed']} failed, "
                  f"{status['skipped']} skipped, {status['not_collected']} not collected "
                  f"(pytest exit {test_run['exit_code']}); passing references do not prove complete coverage")
            if test_run["error"]:
                print("TEST RUN ERROR:", test_run["error"])
        one_shots = sum(item["one_shot"] for item in report["services"])
        print(f"Services: {len(report['services'])} discovered, {one_shots} one-shot; restart not verified for all")
        print(f"Entry points: {len(report['entry_points'])} candidates; manual classification required")
        command_status = Counter(item["status"] for item in report["admin_commands"])
        print(f"Admin command forms: {command_status['contract_present']} contract-present, "
              f"{command_status['missing']} missing, {command_status['unclassified']} unclassified")
        tui_status = Counter(item["status"] for item in report["tui_backend_methods"])
        print(f"TUI backend methods: {tui_status['contract_present']} mapped, "
              f"{tui_status['client_local']} local-only, {tui_status['missing']} missing, "
              f"{tui_status['unclassified']} unclassified")
        tool_status = Counter(item["status"] for item in report["model_tools"])
        print(f"Model tools: {tool_status['contract_present']} mapped, {tool_status['agent_mediated']} agent-mediated, "
              f"{tool_status['worker_internal']} worker-internal, {tool_status['missing']} missing, "
              f"{tool_status['unclassified']} unclassified")
        for item in report["settings"]:
            if item["status"] == "missing":
                print("MISSING setting:", item["name"])
            elif item["status"] == "restricted":
                print("RESTRICTED setting:", item["name"], "-", item["restriction"])
        for item in report["requirements"]:
            if item["status"] == "missing":
                print("MISSING feature:", item["name"], "(" + item["source"] + ")")
        for item in report["executor_reads"]:
            if item["status"] == "missing":
                print("MISSING private executor read:", item["name"])
        for item in report["deployment_variables"]:
            if item["status"] == "missing":
                print("MISSING Compose variable:", item["name"])
            elif item["status"] == "restricted":
                print("RESTRICTED Compose variable:", item["name"], "-", item["restriction"])
        for item in report["runtime_variables"]:
            if item["status"] in {"unclassified", "unsupported_deployment"}:
                print(item["status"].upper(), "runtime variable:", item["name"],
                      "-", item["reason"] or item["sources"][0])
        for item in report["services"]:
            if item["restart"] == "blocked":
                print("BLOCKED restart:", item["name"])
            if not item["healthcheck_defined"] and not item["one_shot"]:
                print("NO declared healthcheck:", item["name"])
        for limitation in report["limitations"]:
            print("UNVERIFIED:", limitation)
        for message in report["coverage_failures"]:
            print("COVERAGE GAP:", message)
    return int(bool(report["coverage_failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
