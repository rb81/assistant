"""Plain-language help for the local administration interface."""
import re

GROUP_HELP = {
    "identity": "Name your assistant and choose the timezone it uses.",
    "approvals": "Choose where to receive requests that need your approval. The approval email is your address, "
                 "not the assistant's mailbox.",
    "models": "Choose the model provider and models used for ordinary tasks. Enter provider credentials and "
              "the bundled database password. Model use can cost money; task limits are not a spending cap.",
    "email": "Connect a dedicated assistant mailbox for receiving requests and sending replies. "
             "Intake can move existing messages into archive or quarantine folders. Do not use your personal inbox.",
    "telegram": "Connect a Telegram bot and restrict who may use it. Numeric user IDs control access; "
                "verified email-to-ID mappings let the assistant address known recipients and send approvals.",
    "calendar": "Connect Google, Outlook or Zoho calendars using OAuth credentials. This permits real calendar "
                "actions. Reading existing event details is controlled separately under Advanced > Calendar privacy.",
    "zoom": "Connect Zoom so the assistant can manage meetings. Choose server-to-server authorization for "
            "an account-owned app, or OAuth for a user-authorized app, then supply matching credentials.",
    "dropbox": "Exchange files through a private Dropbox App Folder, not your whole Dropbox. "
               "Imports and exports are separate permissions; exports can send local files to Dropbox.",
    "transcription": "Convert incoming voice messages to text using the local transcription service. "
                     "This is speech recognition, not speech generation. Enabling it adds a Docker service.",
    "embeddings": "Configure semantic search: an embedding model turns content into searchable numeric vectors. "
                  "Local Ollama is the default. A remote endpoint receives the content being indexed.",
    "security": "Choose the development supervisor model and session limits, configure admin-email authentication "
                "and optional content screening for messages and scripts. "
                "Screening is an extra check, not a guarantee of safety or a replacement for script approvals.",
    "advanced:Calendar privacy": "Choose whether the assistant can read private details of existing events, "
                                 "including titles, descriptions, locations and attendees. Disabled by default; "
                                 "enabling this does not grant permission to edit or delete those events.",
    "advanced:Task limits": "Bound the model iterations, tokens, response length and elapsed time a task may use. "
                            "These are work limits, not a reliable dollar cap. Set spending limits with your provider.",
    "advanced:Email behavior": "Control Sent-folder copies, inbox polling, outgoing-email rate limits and "
                               "attachment counts. Mailbox credentials and access rules are under Connections > Email.",
    "advanced:Memory and workspace indexing": "Control automatic memory upkeep, how many memories enter prompts, "
                                              "email-context lookback and file indexing. Choose the embedding model "
                                              "separately under Search & embeddings.",
    "advanced:Web search": "Allow internet search, limit result counts, and optionally restrict or exclude domains. "
                           "This is separate from network access for scripts running in the sandbox.",
    "advanced:Sandbox network": "Choose HTTPS hosts sandbox commands may reach without asking you first. "
                                "Other hosts require approval. Broad host rules reduce isolation; keep this list narrow.",
    "advanced:Model preset tool": "Allow the assistant to use optional OpenRouter text, image and audio presets. "
                                  "Choose data-retention restrictions and a budget shared with text-to-speech. "
                                  "This budget does not cover ordinary task-model calls.",
    "advanced:Text-to-speech": "Generate spoken audio from text using OpenRouter. Choose a speech model and "
                               "compatible voice. This sends text to a provider, costs money and shares the "
                               "Model preset tool budget; it is separate from local voice transcription.",
    "advanced:Approvals and names": "Choose where human approval requests are delivered and the model used "
                                    "to generate short task and workstream names. Telegram approvals require "
                                    "an allowlisted numeric ID mapped to the administrator's email. Failed or "
                                    "uncertain Telegram delivery falls back to email when SMTP is configured; "
                                    "enable email intake to receive confirmation replies.",
    "advanced:Research and notifications": "Control the separate deep-research workflow and its tool-call limit. "
                                           "Optionally enable periodic administrator digest messages.",
}

PAGE_HELP = {
    "Model settings": "See all saved model roles and tuning together, including disabled features. "
                      "Search or open an existing editor; local drafts, running models and provider availability "
                      "are not shown as verified state.",
    "Home": "Start setup here, or jump to services, saved records and settings. Edits remain in a draft "
            "until you confirm Review & save. Saving configuration does not start the app.",
    "Services": "See actual Docker component state and health. Open a component for logs or individual controls. "
                "Start app uses saved settings and may begin processing existing requests; Stop app preserves data.",
    "Service": "Inspect this Docker component, read its logs, or start, stop, restart or recreate it. "
               "Apply settings uses saved configuration, not unsaved drafts. Changes may interrupt active work.",
    "Logs": "Read the selected component's latest 150 log lines. Live mode refreshes every five seconds. "
            "Tab into the log text and use arrows or Page Up / Page Down to scroll; no service is changed.",
    "Activity": "Browse live tasks, stored memories, reminders, projects and registered scripts. Opening a list "
                "or record does not change it. Changes to live records require confirmation.",
    "Tasks": "Inspect queued, running, completed or failed requests. Open a task for its result, event history "
             "and controls such as retry, instruction or cancellation. Retrying may repeat external actions.",
    "Memories": "Inspect persistent facts and preferences used by the assistant. Open a memory to edit, pin, "
                "delete or handle a suggestion. Memory changes affect future context, not completed actions.",
    "Reminders": "Inspect scheduled follow-up tasks and recurring reminders. Open one to cancel future execution. "
                 "Cancellation cannot undo a reminder that has already run.",
    "Projects": "Browse projects and workstreams that group conversations, tasks and related objects. Open one "
                "to inspect its objects or preview deletion. Shared objects are preserved when deleting a project.",
    "Scripts": "Inspect registered scripts, their exact code versions and permission grants. Open a version "
               "to block its script, unblock it or revoke approvals. Unblocking does not itself approve execution.",
    "Events": "Inspect the selected task's recorded events, including tool inputs, outputs, token use and timing "
              "when available. These are historical records; reading them does not rerun tools.",
    "Objects": "Browse records attached to this project or workstream. Objects can be shared with other projects. "
               "Opening an object shows stored details without changing its attachments.",
    "Record": "Inspect the selected record. Tab into a text area to scroll its full contents. Available actions "
              "depend on the record type and change live data only after confirmation.",
    "Connections": "Choose the channels and optional accounts the assistant can use. Enabled labels describe "
                   "your draft settings, not a successful connection test. Enable only integrations you need.",
    "Settings": "Configure identity, model access, safety, search and instructions. Credentials, maintenance "
                "and recovery have separate controls. Form changes stay in your draft until reviewed and saved.",
    "Settings:assistant": "Set identity, timezone, standing instructions and everyday assistant behavior.",
    "Settings:models": "Choose model access, speech and search models. Provider use may send data externally and cost money.",
    "Settings:connections": "Configure optional accounts and channels. Saved credentials do not verify connectivity.",
    "Settings:safety": "Review permissions, approval delivery, screening and allowed destinations.",
    "Settings:server": "Manage services, diagnostics, storage and recovery. Starting services can process existing requests.",
    "Advanced": "Tune privacy, task limits, email behavior, memory, web search, script networking, extra model "
                "tools, speech and notifications. Move to an option to see what it controls before opening it.",
    "Skills": "Search the skill catalog. Shipped skills are locked; add a custom skill or a preference overlay.",
    "Skill": "Read exact instructions and revision. Custom skills can be edited or deleted after confirmation.",
    "Instructions": "Edit the assistant's standing instructions: its role, preferred behavior and boundaries. "
                    "These are guidance, not enforced security permissions. Keep draft retains edits in memory; "
                    "Review & save writes them to data/config/AGENT.md.",
    "Review": "Review effective settings, hidden-credential changes and all validation problems. Fix listed "
              "issues before saving. Save writes local files but does not restart workers or activate new credentials.",
    "Credentials": "Manage protected passwords, API keys and tokens without displaying saved values. Replacements "
                   "must be activated in affected services. Database passwords use a coordinated rotation instead "
                   "of a simple replacement; save or discard configuration drafts first.",
    "Storage": "Move credentials between protected host files and a private Docker volume, recover interrupted "
               "operations, or reset the bundled database. Clean install also deletes shared files and private "
               "attachments while keeping settings and credentials. Neither storage choice encrypts credentials. "
               "Docker administrators can access them. Back up first; resets do not create a backup.",
    "Pending deletions": "Find previously confirmed project deletions that did not finish. Resume retries their "
                         "cleanup. Writer services deliberately remain stopped when completion cannot be verified.",
    "Project deletion": "Inspect the exact deletion inventory before confirming. Unshared project objects are "
                        "deleted; shared objects are retained elsewhere. Writers pause during cleanup, and a "
                        "changed inventory requires a new preview. No backup is created.",
    "Search": "Inspect the search index, test the embedding-model connection or reindex missing/incompatible "
              "vectors. Reindexing preserves source records but may take time and use a remote model if configured.",
    "Help": "Move with Tab / Shift-Tab or click a control. The help panel explains the focused option; "
            "F1 opens its full help without losing edits. Esc goes back, F5 refreshes and Ctrl-Q exits. "
            "For questions about the system, capabilities or integrations, start with docs/README.md. "
            "It links to functionality, configuration, safety and recovery guides.",
}

SERVICE_HELP = {
    "task-agent": "Performs queued tasks with model calls and tools. Starting it can take real external actions "
                  "and incur provider charges.",
    "postgres": "Stores tasks, memories, reminders, permissions and other application records. "
                "Stopping it prevents database-backed components from working.",
    "downloader": "Polls the dedicated IMAP mailbox, checks incoming mail and creates requests. "
                  "It can move messages into archive or quarantine folders.",
    "workspace-indexer": "Indexes supported workspace files and refreshes searchable content and vectors "
                         "during idle periods. It uses the configured embedding endpoint.",
    "reminder-scheduler": "Checks due reminders and queues their follow-up tasks. Stopping it delays scheduled work.",
    "deep-research-agent": "Processes deep-research jobs using model access and research tools. "
                          "It is separate from the ordinary task worker.",
    "supervisor": "Monitors running tasks for stalled or overdue work and handles task-specific instructions.",
    "heartbeat": "Performs periodic housekeeping and sends administrator digests when configured.",
    "telegram-receiver": "Receives bot updates from allowlisted Telegram users and creates incoming requests.",
    "request-preprocessor": "Prepares received requests for task processing, including configured safety checks.",
    "clamav": "Scans incoming files for known malware. This is an extra safeguard, not a guarantee of safe content.",
    "ollama": "Hosts the bundled local embedding model for semantic search without sending content "
              "to an external embedding provider.",
    "ollama-model-puller": "Downloads and prepares the configured local embedding model. This is an "
                           "initialization component; exiting successfully is normal.",
    "transcription": "Converts incoming voice audio into text locally. It does not generate spoken responses.",
    "web-reader": "Starts a fresh Chromium process for each uncached page and closes it after extraction.",
    "web-reader-gateway": "Connects Assistant workers to the managed browser and brokers public HTTPS egress.",
    "transcription-init": "Prepares the local voice-transcription model and runtime. This is an "
                          "initialization component; exiting successfully is normal.",
    "sandbox": "Runs approved scripts in isolated containers and enforces their network permissions. "
               "The broker has privileged Docker access; stopping it interrupts sandbox work.",
    "sandbox-init": "Prepares the script-runner environment. This is an initialization component; "
                    "exiting successfully is normal.",
}

ACTION_HELP = {
    "Previous": "Show the previous page of records. This does not change application data.",
    "Next": "Show the next page of records. This does not change application data.",
    "Refresh": "Reload the current page from saved configuration or running services. Your configuration draft "
               "is preserved. A refresh does not save edits or restart components.",
    "Refresh status": "Read current Docker component state and health again without restarting it.",
    "Retry screen": "Try rendering this page again after resolving the reported problem. Your draft is preserved.",
    "Back": "Return to the previous page. Form edits are retained in the draft unless you choose Discard this form.",
    "Retry": "Reload configuration from the Management API after resolving the displayed startup error.",
    "Quit": "Exit the terminal client. Running services continue. You will be asked before discarding edits.",
    "[x] I understand the responsibilities": "Withdraw the first-run acknowledgement. Saving setup requires "
                                             "accepting that the assistant can take real actions and send data to providers.",
    "[ ] I understand the responsibilities": "Acknowledge the first-run warnings about real actions, provider "
                                             "data sharing, narrow permissions and backups. This does not save or start anything.",
    "Review & save": PAGE_HELP["Review"],
    "Keep draft": "Keep this form's edits in memory and return to the previous page. Nothing is written to disk "
                  "or applied to services. Use Review & save to persist the draft.",
    "Discard this form": "Discard changes made in this open form and return. Previously kept changes in other "
                         "forms remain in the draft. Saved files and running services are unchanged.",
    "Save configuration": "After confirmation, write the reviewed draft, instructions and credentials to local "
                          "configuration. Saving does not start or recreate services. Apply saved changes separately.",
    "Discard all draft changes": "After confirmation, reload saved configuration and discard all unsaved edits. "
                                 "Saved files and live application records are unchanged.",
    "Generate secure database password": "Generate a random password into this masked form for a fresh database. "
                                         "Keep draft to retain it, then Review & save. Existing databases must use "
                                         "their current password or the coordinated rotation in Credentials.",
    "Move credentials to a Docker volume": "Save credentials in a private Docker volume and recreate affected "
                                           "services to use it. Expect downtime. Back up the volume separately; "
                                           "Docker administrators can still read its contents.",
    "Move credentials to host files": "Save credentials as protected files under data/.secrets and recreate "
                                      "affected services. The old volume is retained until its mounts can be safely retired.",
    "Finish credential storage change": "Retry activation and retirement left incomplete by a previous move to "
                                        "host files. Keep the old credential volume until the operation succeeds.",
    "Recover an interrupted database password rotation": "Restore the previous database password and recreate "
                                                         "its consumers after an interrupted rotation. Save or discard "
                                                         "your draft first; recovery may interrupt running services.",
    "Reset bundled database": "Permanently destroy and rebuild all bundled PostgreSQL data: tasks, memories, "
                               "reminders and other database records. Settings, credentials and workspace files "
                               "remain. Keep mailbox position is enabled by default to avoid reprocessing handled "
                               "emails; disable it to re-read the mailbox. Queued Telegram updates are skipped. "
                               "No backup is made. Custom databases are refused. Requires typing RESET.",
    "Clean install (also delete files)": "Rebuild the bundled database AND delete everything in data/share/ and "
                                         "data/private/artifacts/: shared and generated files, attachments, caches "
                                         "and trash. Settings, credentials, TLS certificates and data/config/AGENT.md "
                                         "remain. Keep mailbox position is enabled by default; disable it to re-read "
                                         "the mailbox. Queued Telegram updates are skipped. "
                                         "Other service data is untouched. No backup is made. Custom databases "
                                         "are refused. Requires typing RESET.",
    "Delete previewed project": PAGE_HELP["Project deletion"],
    "Keep project": "Leave this deletion preview without deleting anything. No project data is changed.",
    "Check pending": "Read the inventory of incomplete project deletions without resuming or deleting anything.",
    "Check status": "Read search-index health and counts. This does not start a reindex or change source records.",
    "Verify model": "Send an embedding probe to the configured endpoint to check connectivity and model output. "
                    "A remote endpoint may charge for the request. This does not index your files.",
    "Reindex": "After confirmation, regenerate missing or incompatible search vectors. Source records are "
               "preserved. Work can take time and may send indexed content to the configured embedding provider.",
    "Start app": "Build and start enabled components using saved settings. Startup can process messages "
                 "already in the assistant mailbox or Telegram queue and incur provider charges.",
    "Stop app": "Stop the application's components after confirmation. Active work is interrupted, but database "
                "and workspace data are preserved. Closing this TUI alone does not stop the app.",
    "Start": "Start this component and any required dependencies using saved configuration. Starting a worker "
             "can process waiting requests. Unsaved draft settings are not used.",
    "Restart": "Stop and start this component. Active work may be interrupted. A restart does not recreate "
               "its container with changed settings; use Apply settings for that.",
    "Apply settings": "Recreate this component using saved settings, including changed credentials where "
                      "applicable. Active work may be interrupted. Save configuration drafts first.",
    "Stop": "Stop this component after confirmation. Data is preserved, but dependent components may lose "
            "functionality and active work may be interrupted.",
    "Add credential": "Choose a supported credential name, then enter its masked replacement. This saves a live "
                      "credential separately from configuration drafts. Activate pending changes afterward.",
    "Activate pending": "Recreate affected running services to use pending credential replacements. "
                         "Brief downtime is expected. Save or discard configuration drafts first.",
    "Search": "Filter this collection by its title, task summary, memory content or script entry. Enter up to "
              "500 characters; blank shows all records. This searches stored records, not the web.",
    "New memory": "Create a persistent memory after entering text and confirming. It can influence future "
                  "assistant responses; it does not change the assistant's standing instructions.",
    "Live": "Refresh the selected component's logs every five seconds while this page is open.",
    "Pause live": "Stop automatic log refresh. Existing log text remains readable; the service keeps running.",
    "Retry task": "Requeue this task after confirmation. Review its history first: emails, file changes and "
                  "other external actions already performed are not undone and could be repeated.",
    "Add instruction": "Send additional guidance to this task after confirmation. This is task-specific; "
                       "it does not change the assistant's standing instructions.",
    "Cancel task": "Request cancellation of this task. Completed external actions cannot be undone. "
                   "Inspect its current state and event history afterward.",
    "Approve review": "Approve this task's needs-review state, increase its task limits and requeue it to continue. "
                      "Inspect the reason for review first; this is not a blanket grant of script or network permissions.",
    "Edit": "Replace this memory's content after confirmation. The new text affects future context, "
            "not responses or actions already completed.",
    "Pin": "Keep this memory from ordinary automatic retirement. Pinning does not guarantee inclusion in "
           "every prompt; the configured memory-selection limit still applies.",
    "Unpin": "Remove this memory's pinned protection so normal memory management can handle it. "
             "This does not delete the memory.",
    "Delete": "Permanently delete this memory after confirmation. Future retrieval will not find it; "
              "past messages and other copies are unchanged.",
    "Approve suggestion": "Accept this suggested memory as active so it can be used as future context.",
    "Reject suggestion": "Mark this suggested memory as rejected rather than adopting it as active context.",
    "Cancel reminder": "Cancel future execution of this reminder after confirmation. This cannot undo "
                       "a reminder or external action that has already run.",
    "Preview deletion": "Read a deletion inventory for this project before any destructive confirmation. "
                        "The preview itself deletes nothing and identifies objects retained through sharing.",
    "Block": "Block this script and request that active sandbox runs be stopped after confirmation. "
             "If stopping fails, stop the sandbox service before relying on the block.",
    "Unblock": "Remove this script's block after confirmation. Existing permission rules still apply; "
               "this does not create a new execution approval.",
    "Revoke approvals": "Remove the script's version-bound approvals and request active sandbox runs to stop. "
                        "If stopping fails, stop the sandbox service before relying on revocation.",
}
ACTION_HELP["Recover database credential"] = ACTION_HELP["Recover an interrupted database password rotation"]

FIELD_HELP = {
    "TASK_AGENT_WORKERS": "Number of task-worker replicas started by Compose. More workers can "
                          "process tasks concurrently but increase memory use and simultaneous model "
                          "spending; zero pauses task processing.",
    "REQUEST_PREPROCESSOR_WORKERS": "Number of request-preprocessor replicas started by Compose. "
                                    "Increase for intake backlogs, allowing for more database "
                                    "activity and concurrent screening calls; zero pauses "
                                    "preprocessing.",
    "LOG_LEVEL": "Minimum severity written to application logs. Use DEBUG temporarily for "
                 "troubleshooting; detailed logs may contain sensitive task information and require "
                 "more disk space.",
    "ASSISTANT_MANAGEMENT_PORT": "Host TCP port for the loopback-only management API. Choose an "
                                 "unused port; applying a change interrupts the management connection "
                                 "and clients must reconnect to the new port.",
    "SANDBOX_IMAGE": "Docker image used for the sandbox broker and per-command containers. Use a "
                     "trusted, compatible image; its code runs commands and the broker has powerful "
                     "Docker access.",
    "SANDBOX_ISOLATION_MODE": "Use container for per-command container isolation. Process mode runs "
                              "commands as broker subprocesses without that isolation and is for "
                              "development only; it weakens security substantially.",
    "SANDBOX_RUN_NETWORK_MODE": "Network mode for command containers. per_run_bridge supports "
                                "isolated per-run proxy policy; none disables networking, while "
                                "bridge or existing can bypass that policy and weaken isolation.",
    "SANDBOX_RUN_NETWORK": "Name of an existing Docker network to join when network mode is existing. "
                           "Use only a trusted network; shared connectivity can expose other "
                           "containers and bypass isolated proxy policy.",
    "SANDBOX_EGRESS_MODE": "Outbound policy for per-run bridge networking. proxy uses a restricted "
                           "egress proxy; open allows unrestricted outbound traffic and weakens "
                           "script network controls.",
    "SANDBOX_RUN_CAP_DROP": "Comma-separated Linux capabilities removed from command containers. "
                            "Dropping more reduces kernel privileges but may break commands; removing "
                            "restrictions weakens isolation.",
    "SANDBOX_RUN_CPUS": "CPU quota for each command container, measured in CPU cores and allowing "
                        "fractions. Lower to protect host capacity; higher values can speed CPU-heavy "
                        "scripts but increase contention.",
    "SANDBOX_RUN_MEMORY_BYTES": "Memory limit in bytes for each command container. Raise for "
                                "memory-heavy scripts; too low can terminate commands, while higher "
                                "limits leave less memory for the host and other services.",
    "SANDBOX_RUN_PIDS_LIMIT": "Maximum processes and threads allowed in each command container. Keep "
                              "bounded to limit process exhaustion; raise only for scripts that need "
                              "more parallel workers.",
    "SANDBOX_DEFAULT_TIMEOUT_SECONDS": "Default command runtime in seconds when a broker request "
                                       "omits a timeout, still capped by the maximum timeout. Longer "
                                       "runtimes keep sandbox resources occupied longer.",
    "SANDBOX_MAX_TIMEOUT_SECONDS": "Upper bound in seconds for each command's runtime, even when a "
                                   "longer timeout is requested. Raise only for trusted long-running "
                                   "work; lower bounds reduce resource exposure.",
    "SANDBOX_PROXY_MEMORY_BYTES": "Memory limit in bytes for each per-run egress proxy container. "
                                  "Raise if legitimate traffic exhausts the proxy; higher limits "
                                  "increase memory needed for concurrent runs.",
    "EGRESS_PROXY_IDLE_TIMEOUT": "Seconds without data before an egress proxy connection closes. "
                                 "Increase for legitimate long pauses; longer waits retain idle "
                                 "connections and proxy resources.",
    "EGRESS_PROXY_MAX_CONNECTIONS": "Maximum concurrent connections handled by each run's egress "
                                    "proxy. Raise for scripts with many parallel requests, allowing "
                                    "for more network and proxy resource use.",
    "SANDBOX_RUN_RUNTIME": "Optional Docker runtime for command containers, such as an installed "
                           "isolation runtime. Blank uses Docker's default; a named runtime must "
                           "already be installed on the host.",
    "SANDBOX_RUN_USER": "User or UID:GID passed to command containers. Blank uses the image's "
                        "configured user; choose a least-privileged identity with workspace access to "
                        "avoid permission errors and unnecessary privileges.",
    "SANDBOX_RUN_TMPFS_SIZE": "Size limit for each temporary filesystem mounted in command "
                              "containers, using Docker units such as m or g. Larger limits support "
                              "bigger temporary files but can consume more host memory.",
    "SANDBOX_RUN_DNS_SERVERS": "Comma-separated DNS server addresses for command containers when "
                               "isolated proxy egress is inactive. Proxy mode overrides DNS; use "
                               "trusted resolvers because DNS can expose queried hostnames.",
    "SANDBOX_RUN_SECURITY_OPT": "Comma-separated Docker security options for command containers. Keep "
                                "no-new-privileges enabled to prevent privilege gains; removing "
                                "safeguards weakens sandbox isolation.",
    "SANDBOX_RUN_CONTAINER_PREFIX": "Name prefix for temporary command and proxy containers. Use a "
                                    "recognizable prefix for operations and troubleshooting; changing "
                                    "it does not rename existing containers.",
    "SANDBOX_RUN_NETWORK_PREFIX": "Name prefix for temporary per-run and egress Docker networks. Use "
                                  "a recognizable prefix to distinguish sandbox resources; changing "
                                  "it does not rename existing networks.",
    "ASSISTANT_INSTALL_ROOT": "Absolute host path to this installation, selected by bootstrap for "
                              "management mounts and operations. It is not editable in-band; changing "
                              "it externally can disconnect management from its files.",
    "SANDBOX_SHARED_ROOT_HOST": "Host workspace path mounted into sandbox runs, derived from the "
                                "installation location. It is not editable in-band; pointing it "
                                "elsewhere can expose unrelated host files to scripts.",
    "ASSISTANT_UID": "Host user ID selected by bootstrap to preserve file ownership for "
                     "administration. It is not editable in-band; mismatched IDs can make "
                     "configuration and credential files inaccessible.",
    "ASSISTANT_GID": "Host group ID selected by bootstrap to preserve file ownership for "
                     "administration. It is not editable in-band; mismatched IDs can break "
                     "shared-file access or expose files to the wrong group.",
    "ASSISTANT_DOCKER_SOCKET": "Host Docker socket mounted into the management executor, selected by "
                               "bootstrap. It is not editable in-band; socket access is highly "
                               "privileged and a wrong path can disable service management.",
    "ASSISTANT_MANAGEMENT_IMAGE": "Docker image used for the management API and executor, selected "
                                  "during bootstrap. It is not editable in-band; use only trusted "
                                  "images because the executor has privileged installation and Docker "
                                  "access.",
    "COMPOSE_PROJECT_NAME": "Compose project name that identifies this installation's containers and "
                            "resources. It is not editable in-band; changing it externally can orphan "
                            "existing resources rather than migrate them.",
    "AGENT_SECURITY_DEVELOPMENT_MODEL": "Leave blank to reuse the agent model, provider and credential. "
                                            "Otherwise enter an OpenRouter model ID such as openai/gpt-sol-6.1; "
                                            "this requires an OpenRouter API key under Models & access. "
                                            "Presets are not used. The supervisor uses its own tool-free instructions "
                                            "and does not fall back to another model.",
    "AGENT_SECURITY_DEVELOPMENT_MAX_MINUTES": "Positive integer limiting the duration an administrator may authorize "
                                           "for a development session. Expiry stops further development execution.",
    "AGENT_SECURITY_DEVELOPMENT_MAX_RUNS": "Positive integer limiting the executions an administrator may authorize "
                                        "per development session. Each dispatched attempt consumes one run.",
    "AGENT_IDENTITY_NAME": "The name used for the assistant's identity in prompts and communications.",
    "AGENT_APP_TIMEZONE": "Timezone for local dates and scheduling. Use an IANA name such as Europe/London or "
                      "America/New_York, or UTC; not a city abbreviation such as EST.",
    "AGENT_ADMIN_NAME": "The administrator's display name, used as identity context for the assistant.",
    "AGENT_ADMIN_EMAIL": "The administrator address used for approvals and administrative messages. For Telegram "
                   "approvals, map this exact email to an allowed numeric Telegram user ID.",
    "AGENT_ORG_NAME": "Organization name supplied as context to the assistant. Leave blank if not applicable.",
    "AGENT_ORG_SECURITY_EMAIL": "Security contact supplied as organizational context. This is not a separate mailbox login.",
    "AGENT_ORG_INTERNAL_EMAIL_DOMAINS": "Domains the assistant should recognize as internal, separated by commas "
                                  "(example: example.com,subsidiary.com). This is not the outgoing-recipient allowlist.",
    "AGENT_LLM_BASE_URL": "HTTPS base URL of the OpenAI-compatible chat API, such as "
                         "https://openrouter.ai/api/v1. Do not include an API key, query string or chat/completions suffix.",
    "AGENT_LLM_MODEL": "Provider model identifier used for ordinary tasks, for example anthropic/claude-sonnet-4.6 "
                      "on OpenRouter. It must be available to your configured provider account.",
    "AGENT_LLM_FALLBACK_MODEL": "Alternative model used when the primary model cannot complete a request. "
                               "Choose one available through the same provider endpoint.",
    "AGENT_LLM_OPENROUTER_API_KEY": "OpenRouter credential for OpenRouter model access and optional preset or speech tools. "
                          "Create it in your provider account; set account-level spending limits there.",
    "AGENT_LLM_OPENAI_API_KEY": "Credential for a non-OpenRouter OpenAI-compatible provider. It must match your Provider API URL.",
    "AGENT_DATABASE_PASSWORD": "Password for the bundled PostgreSQL database. Generate one only for a fresh install. "
                         "For existing data, use its current password; change it through Credentials to coordinate rotation.",
    "SETUP_EMAIL_ENABLED": "Start email intake as part of the app. It can process existing messages and move "
                           "them between folders. Configure a dedicated mailbox and narrow access rules first.",
    "AGENT_EMAIL_IMAP_HOST": "Hostname of the provider's incoming-mail server, for example imap.example.com. "
                 "Use a hostname, not a URL or an email address.",
    "AGENT_EMAIL_IMAP_PORT": "Incoming-mail server port (1-65535). The default 993 uses implicit TLS; "
                 "use the port required by your mail provider.",
    "AGENT_EMAIL_IMAP_USERNAME": "Login for the assistant-owned mailbox, often its complete email address.",
    "AGENT_EMAIL_IMAP_PASSWORD": "Incoming-mail password or app-specific password from your email provider.",
    "AGENT_EMAIL_IMAP_FOLDER": "Folder watched for new requests, usually INBOX. Existing messages here can be processed at startup.",
    "AGENT_EMAIL_IMAP_ARCHIVE_FOLDER": "Folder where processed incoming messages are moved. Use the provider's exact folder name.",
    "AGENT_EMAIL_IMAP_QUARANTINE_FOLDER": "Folder for rejected, held or unaccepted incoming messages. These are moved out of "
                              "intake rather than treated as tasks. Use the provider's exact folder name.",
    "AGENT_EMAIL_IMAP_SENT_FOLDER": "Mailbox folder used for copies of sent messages when Advanced > Email behavior > "
                        "Save outgoing email is enabled. Folder names vary by provider.",
    "AGENT_EMAIL_SMTP_HOST": "Hostname of the outgoing-mail server, for example smtp.example.com. Use a hostname, not a URL.",
    "AGENT_EMAIL_SMTP_PORT": "Outgoing-mail server port (1-65535). Usually 587 for STARTTLS or 465 for implicit TLS; "
                 "follow your provider's instructions.",
    "AGENT_EMAIL_SMTP_USERNAME": "Login authorized to send from this mailbox. It may be the same as the incoming-mail login.",
    "AGENT_EMAIL_SMTP_PASSWORD": "Outgoing-mail password or app-specific password. Supply it even if it matches the IMAP password.",
    "AGENT_EMAIL_SMTP_FROM": "Sender address placed on outgoing mail. Your SMTP provider must permit this address.",
    "AGENT_IDENTITY_EMAIL": "The assistant's email identity. Normally use the dedicated mailbox address, "
                   "not the administrator's approval address.",
    "AGENT_EMAIL_ACTIONABLE_SENDERS": "Comma-separated sender rules allowed to create tasks, such as you@example.com. "
                                "Required when email is enabled. A single * allows everyone. Keep this narrow; sender matching "
                                "alone does not prove a sender's identity.",
    "AGENT_EMAIL_ALLOWED_RECIPIENT_DOMAINS": "Comma-separated domains to which outgoing mail may be sent, such as "
                                      "example.com. Required when email is enabled; * permits any domain. "
                                      "The administrator email remains allowed. This controls real outbound data sharing.",
    "AGENT_TELEGRAM_ENABLED": "Start the Telegram receiver for the configured bot. Only allowlisted numeric users "
                        "may use it; enabling the checkbox alone does not verify connectivity.",
    "AGENT_TELEGRAM_BOT_TOKEN": "Bot API token issued by Telegram's BotFather. Treat it as a password for the bot.",
    "AGENT_TELEGRAM_ALLOWED_USER_IDS": "Comma-separated positive numeric Telegram user IDs, such as 123456789. "
                                 "Use IDs, not usernames or phone numbers. Only listed users can submit requests.",
    "AGENT_TELEGRAM_RECIPIENTS": 'JSON object mapping verified emails to numeric Telegram IDs, for example '
                           '{"you@example.com":"123456789"}. Verify each mapping yourself. Administrator approvals '
                           "require the admin email to map to an allowlisted ID.",
    "AGENT_CALENDAR_ENABLED": "Allow calendar tools using the configured accounts. Complete the selected provider's "
                        "OAuth credentials first. Actions can change real calendars.",
    "AGENT_CALENDAR_DEFAULT_PROVIDER": "Calendar account used when a request does not select one. "
                                 "Only accounts with complete saved OAuth credentials are listed. "
                                 "Save a new account first, then choose it here. Connectivity is not verified by this list.",
    "AGENT_CALENDAR_OUTLOOK_TENANT_ID": "Microsoft tenant used for OAuth, usually common for supported personal and "
                                 "organizational accounts, or your directory's tenant ID.",
    "AGENT_CALENDAR_ZOHO_API_BASE": "HTTPS Calendar API URL for your Zoho region. Match the region used for the OAuth "
                             "account; the default is https://calendar.zoho.com/api/v1.",
    "AGENT_ZOOM_ENABLED": "Allow meeting-management tools to use your Zoom app. This grants access to real meetings.",
    "AGENT_ZOOM_AUTH_MODE": "Use s2s for Server-to-Server OAuth (requires account ID), or oauth for a "
                      "user-authorized app (requires refresh token). Both need client ID and client secret.",
    "AGENT_ZOOM_CLIENT_ID": "Client ID from the Zoom app matching your selected authorization mode.",
    "AGENT_ZOOM_CLIENT_SECRET": "Client secret from your Zoom app. Keep it private; it is used to obtain access tokens.",
    "AGENT_ZOOM_ACCOUNT_ID": "Zoom account ID required for server-to-server (s2s) authorization; not a meeting ID.",
    "AGENT_ZOOM_REFRESH_TOKEN": "Refresh token from user-authorized Zoom OAuth. Required for oauth mode, not s2s mode.",
    "AGENT_DROPBOX_ENABLED": "Allow explicit file exchange with the app's private Dropbox App Folder. This does not "
                       "grant access to your entire Dropbox or automatically publish workspace files.",
    "AGENT_DROPBOX_ALLOW_EXPORTS": "Standing permission for any task to export eligible local files to the Dropbox "
                             "App Folder without extra per-upload confirmation. Leave off for import-only use.",
    "AGENT_DROPBOX_MAX_FILE_BYTES": "Maximum size of an exchanged file, in bytes. Default 26214400 is 25 MiB. "
                              "Larger files are refused; choose a positive limit.",
    "AGENT_DROPBOX_CLIENT_ID": "App key/client ID for a Dropbox app configured with App Folder access, not Full Dropbox.",
    "AGENT_DROPBOX_REFRESH_TOKEN": "Offline-access OAuth refresh token for the Dropbox App Folder app. "
                             "It must belong to the configured client ID.",
    "AGENT_TRANSCRIPTION_ENABLED": "Run the local speech-recognition service for incoming voice messages. "
                             "It consumes local compute and downloads its model; it does not generate spoken replies.",
    "AGENT_TRANSCRIPTION_LANGUAGE": "Use auto to detect the language, en for English, or ar for Arabic. "
                              "These are the supported choices; other language codes are not accepted.",
    "AGENT_EMBEDDINGS_ENABLED": "Generate vectors for semantic retrieval. With this disabled, vector search is "
                               "unavailable; keyword retrieval can still be used. File indexing has its own switch.",
    "AGENT_EMBEDDINGS_MODEL": "Embedding-model identifier supported by the endpoint. Local default is embeddinggemma. "
                             "Changing models may require reindexing missing or incompatible vectors.",
    "AGENT_EMBEDDINGS_BASE_URL": "Use http://ollama:11434 for bundled local Ollama. Remote endpoints require HTTPS "
                                "and receive the content being embedded; choose a trusted provider.",
    "AGENT_EMBEDDINGS_DIMENSIONS": "Number of values per vector; blank uses the model default. EmbeddingGemma "
                                  "supports 128, 256, 512 or 768. Other models must support the requested size.",
    "AGENT_EMBEDDINGS_TIMEOUT_SECONDS": "Positive time limit in seconds for an embedding request. Raise it if "
                                       "your local model is too slow; it is not a limit on the whole reindex operation.",
    "AGENT_SECURITY_EMAIL_AUTHENTICATION_ENABLED": "Recommended; on by default. Email claiming your admin address must pass "
                                         "an aligned DKIM signature or SPF check, or it is quarantined. A matching From "
                                         "address alone does not prove ownership. Turn off only if your mail provider "
                                         "cannot pass either check.",
    "AGENT_SECURITY_CONFIRM_NEW_ADMIN_CONVERSATIONS": "Confirm fresh admin task emails with no established reply "
                                                     "or conversation history. Replies and continuations from known "
                                                     "participants proceed without routine confirmation. Turning this "
                                                     "off does not disable sender checks, screening or action approvals.",
    "AGENT_SECURITY_DECISION_MODEL_ENABLED": "Send configured content to the screening provider for a safety decision. "
                                       "This may share sensitive text and incur charges. It does not replace approvals.",
    "AGENT_SECURITY_DECISION_MODEL_PROVIDER": "Screening adapter to use: openrouter, perplexity, jev, clef, openai or custom. "
                                        "Its endpoint, model and API credential must match the chosen provider.",
    "AGENT_SECURITY_DECISION_MODEL_URL": "HTTPS screening endpoint. Leave blank to use the selected provider's default "
                                   "when available. Custom providers need their own compatible endpoint.",
    "AGENT_SECURITY_DECISION_MODEL_MODEL": "Model used for the screening decision, separate from the primary task model. "
                                     "Use an identifier supported by the selected screening provider.",
    "AGENT_SECURITY_DECISION_MODEL_ON_ERROR": "What to do when screening fails: review pauses for human attention, "
                                        "reject refuses, and allow proceeds without a successful screen. "
                                        "Use review or reject if bypassing screening is unacceptable.",
    "AGENT_SECURITY_DECISION_MODEL_SCREEN_OUTBOUND_EMAIL": "Include outgoing email in enabled content screening before sending. "
                                            "Screening provider access and error policy still apply.",
    "AGENT_SECURITY_DECISION_MODEL_SCREEN_SCRIPTS": "Include script content in enabled screening. Human script permissions "
                                     "are still required, and malicious screening findings remain blocked.",
    "AGENT_SECURITY_SCRIPTS_ALLOWED_DOMAINS": "Comma-separated trusted domains used by script-source screening. "
                                            "This is not the sandbox network allowlist and does not grant execution permission.",
    "AGENT_SECURITY_DECISION_MODEL_API_KEY": "Credential for the selected screening provider, separate from normal task "
                                 "model access. Enter a key authorized for the screening endpoint.",
    "AGENT_DATABASE_URL": "Connection URL for an external or custom PostgreSQL database, including its access "
                    "credentials. It overrides the bundled database connection. Use a valid PostgreSQL URL "
                    "with the required TLS settings; the bundled database reset does not support custom databases.",
    "AGENT_SANDBOX_API_TOKEN": "Internal credential authorizing access to the script-runner broker. Normally generated "
                         "during setup; manual replacement must also be activated in all affected services.",
    "AGENT_SANDBOX_REVOKE_TOKEN": "Restricted internal credential used to stop active sandbox runs when permissions "
                            "are revoked. Normally generated during setup; keep its consumer services in sync.",
    "AGENT_TRANSCRIPTION_API_TOKEN": "Internal credential protecting the local voice-transcription API. Normally "
                               "generated during setup; activate a replacement in both the service and its consumers.",
    "AGENT_CALENDAR_POLICY_ALLOW_READ_EVENT_DETAILS": "Off protects existing event titles, descriptions, locations "
                                                     "and attendees from detail-reading tools. On permits reading "
                                                     "those details; it does not grant editing or deletion rights.",
    "AGENT_LIMITS_MAX_ITERATIONS_PER_TASK": "Maximum model/tool-loop iterations for one task. Lower values limit "
                                            "runaway work but may stop complex tasks before they finish.",
    "AGENT_LIMITS_MAX_TOKENS_PER_TASK": "Token-use ceiling for one task. Tokens are pieces of model input/output, "
                                       "not dollars; prices vary by provider and model.",
    "AGENT_LLM_MAX_TOKENS_PER_CALL": "Maximum output tokens requested in each task-model response. "
                                     "Lower limits can truncate long answers or tool arguments.",
    "AGENT_SUPERVISOR_MAX_TASK_DURATION_MINUTES": "Elapsed task duration after which the supervisor treats a "
                                                 "running task as overdue. This is a watchdog threshold, "
                                                 "not an undo of external actions.",
    "AGENT_EMAIL_SAVE_TO_SENT": "Append copies of outgoing mail to the configured Sent folder. Some providers "
                                "already save sent messages; enabling both can produce duplicate copies.",
    "AGENT_EMAIL_IMAP_POLL_INTERVAL_SECONDS": "Seconds between checks for incoming email. A shorter interval reduces latency "
                                "but increases mailbox traffic; use a positive value.",
    "AGENT_LIMITS_MAX_EMAILS_PER_HOUR": "Rate limit for outgoing messages. It limits email volume, not model spending "
                                "or the number of tasks accepted.",
    "AGENT_EMAIL_MAX_ATTACHMENT_COUNT": "Maximum attachments per outgoing email. Zero disables attachments, "
                                         "not email itself. Separate byte-size limits also apply.",
    "AGENT_MEMORY_STEWARD_ENABLED": "Allow automatic memory stewardship to maintain stored facts and preferences. "
                                     "You can still inspect and manage memories manually under Activity.",
    "AGENT_MEMORY_STEWARD_MAX_INJECTED_MEMORIES": "Maximum selected memories included in model prompts. More "
                                                 "memories can improve context but increase token use and data sharing.",
    "AGENT_CONTEXT_SEARCH_DAYS": "How far back, in days, to search stored email context. Larger windows can "
                                  "surface more history and increase the amount of private context available.",
    "AGENT_WORKSPACE_INDEX_ENABLED": "Index supported workspace files so retrieval can find them. "
                                      "Separate from enabling semantic search or choosing its embedding model.",
    "AGENT_SEARCH_ENABLED": "Allow the web-search tool through supported model access. Search queries go "
                             "to an external provider; this does not grant sandbox scripts network access.",
    "AGENT_SEARCH_MAX_RESULTS": "Maximum web results requested per search. More results can increase context "
                                 "size and provider usage; choose a positive count.",
    "AGENT_SEARCH_ALLOWED_DOMAINS": "Comma-separated domains to limit web-search results to; blank permits "
                                     "any domain. This is a search filter, not a sandbox network permission.",
    "AGENT_SEARCH_EXCLUDED_DOMAINS": "Comma-separated domains excluded from web search; blank excludes none. "
                                      "This does not remove existing records or block sandbox networking.",
    "AGENT_SANDBOX_NETWORK_ALLOWED_HOSTS": "Comma-separated HTTPS hosts sandbox commands can reach without approval. "
                                  "*.example.com covers subdomains, not the bare domain. Avoid broad entries; "
                                  "this grants network access, not script execution permission.",
    "AGENT_MODEL_SERVICE_ENABLED": "Enable optional OpenRouter preset tools for text, images and audio. "
                                     "Requires AGENT_LLM_OPENROUTER_API_KEY. Ordinary task-model access is configured separately.",
    "AGENT_MODEL_SERVICE_ZDR_ONLY": "Restrict preset-model selection and routing to supported Zero Data Retention "
                                      "options. This is a provider routing policy, not a claim that data stays local.",
    "AGENT_MODEL_SERVICE_BUDGET_USD": "Rolling USD threshold shared by preset tools and text-to-speech. Checked "
                                       "before each request, so the final request may exceed it. Does not cover "
                                       "ordinary task calls; set hard account limits with your provider.",
    "AGENT_MODEL_SERVICE_BUDGET_PERIOD_DAYS": "Rolling accounting window for the shared preset/speech budget, "
                                               "from 1 to 365 days. This is not a calendar-month reset.",
    "AGENT_TTS_ENABLED": "Allow dedicated text-to-speech through OpenRouter. Requires AGENT_LLM_OPENROUTER_API_KEY "
                         "and uses the shared preset/speech budget. Text leaves this computer for synthesis.",
    "AGENT_TTS_ZDR_ONLY": "Require supported Zero Data Retention speech models and routing. "
                          "This restricts provider use; speech synthesis is still remote.",
    "AGENT_TTS_MODEL": "OpenRouter speech-model slug. The selected voice must be supported by this model. "
                       "List choices with python -m assistant_agent.tts voices in the runtime environment.",
    "AGENT_TTS_VOICE": "Voice identifier supported by the chosen speech model. Voice names are model-specific, "
                       "not arbitrary descriptions such as 'friendly'.",
    "AGENT_APPROVALS_CHANNEL": "Deliver approval requests by email, Telegram, both, or in the Assistant app. Telegram needs "
                              "a bot token and the admin email mapped to an allowlisted numeric ID. "
                              "Choose a channel you will actually monitor.",
    "AGENT_NAMING_MODEL": "Model for generating short job and workstream names, not for performing the main "
                          "task. Use an inexpensive model supported by your provider.",
    "AGENT_DEEP_RESEARCH_ENABLED": "Allow the separate deep-research workflow through supported model access. "
                                    "Research can use multiple tools and provider calls; disabling it does not disable ordinary tasks.",
    "AGENT_DEEP_RESEARCH_MAX_TOOL_CALLS": "Maximum tool calls in a deep-research run. More calls can improve "
                                           "research depth but increase time and cost.",
    "AGENT_HEARTBEAT_ADMIN_DIGEST_INTERVAL_HOURS": "Hours between periodic administrator digests. "
                                                   "Zero disables these digests, not the other health-monitoring services.",
}
for provider in ("GOOGLE", "OUTLOOK", "ZOHO"):
    for suffix, explanation in {
        "CLIENT_ID": "OAuth application client ID from this provider's developer console.",
        "CLIENT_SECRET": "OAuth application client secret. It must belong to the configured client ID.",
        "REFRESH_TOKEN": "Offline-access refresh token obtained by authorizing the calendar account. "
                         "It must match this app and have the calendar scopes you intend to permit.",
        "CALENDAR_ID": "Calendar identifier within the authorized account, not its OAuth client ID. "
                       "Use primary for the main Google calendar, or the provider's applicable calendar ID.",
    }.items():
        FIELD_HELP[f"AGENT_CALENDAR_{provider}_{suffix}"] = f"{provider.title()}: {explanation}"


def page_help(route):
    if route.startswith(("Form:", "Connection:")):
        key = route.split(":", 1)[1]
        if key == "deployment" or key.startswith(("catalog:", "calendar:")):
            return "Advanced configuration from the server catalog. Review defaults and deployment restrictions before saving. Changes may require restarting the affected services."
        return GROUP_HELP[key]
    return PAGE_HELP[route]


def field_help(spec):
    normalized = re.sub(r"^(AGENT_CALENDAR_(?:GOOGLE|OUTLOOK|ZOHO))_[2-9]_", r"\1_", spec.key)
    explanation = FIELD_HELP.get(normalized, spec.label + " (" + spec.key + "). Review this advanced setting before saving.")
    if spec.secret:
        return explanation + " Input is masked. Blank keeps an existing credential; saved values are never shown."
    return explanation + (" Default: " + spec.default + "." if spec.default else "")


def credential_help(name):
    match = re.fullmatch(r"(AGENT_CALENDAR_(?:GOOGLE|OUTLOOK|ZOHO))_([2-9])_(CLIENT_ID|CLIENT_SECRET|REFRESH_TOKEN)", name)
    if match:
        return f"Calendar account {match[2]}. " + FIELD_HELP[match[1] + "_" + match[3]]
    return FIELD_HELP[name]
