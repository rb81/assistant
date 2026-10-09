# Dropbox File Exchange

Dropbox is an optional, on-demand file exchange, not a synchronization service.
One connected account supplies documents from its dedicated app `Inbox`. Tasks
can deliver workspace files to `Outbox/job-<id>` when standing delivery permission
is enabled. Adding a file does not create a task: send the Assistant an email
asking it to use the file.

## Connect

For a self-hosted installation, register a scoped app at
[Dropbox App Console](https://www.dropbox.com/developers/apps). Select **App
Folder**, not **Full Dropbox**, and choose a name such as `Assistant`. Enable only
`files.metadata.read`, `files.content.read`, and `files.content.write` in the
Permissions tab. App Folder is a registration setting, not an OAuth scope; a
folder-prefix restriction is not a substitute for selecting App Folder.

The operator confirms the registration type during setup. The API client always
restricts file operations to Inbox and Outbox, but cannot independently prove
that a supplied token was issued to an App Folder rather than a Full Dropbox app.
For a shared application, its maintainer registers the app once and supplies the
app key; each user authorizes their own account. No client secret is required.

Run:

```bash
./scripts/setup.sh --section dropbox
```

Enable Dropbox, enter the app key, open the displayed authorization URL on any
device, and enter the authorization code in the setup terminal. Authorization
uses PKCE S256 and offline access with automatic refresh of short-lived access
tokens. There is no callback server, public port, or host Dropbox client to
install. Do not send authorization codes or refresh tokens through email/chat.

Choose whether **any task may deliver files to Outbox without additional
confirmation**. This is standing permission, not per-upload approval. The default
is disabled. Review and Save to persist the connection and consent; Cancel
preserves the saved installation. A provider authorization may still exist after
cancellation: revoke it in Dropbox if no longer needed.

Create `Inbox` inside `/Apps/<your-app-name>` using Dropbox before the first
import. `Outbox` and its task subfolder are created on the first authorized
export. Setup's optional verification checks authorization only and creates no
files or folders. Recreate the affected services using the normal upgrade/setup
workflow to activate changes. Back up an existing installation before starting
new code: transfer tracking adds a database table.

## Use

Place a document in `/Apps/<your-app-name>/Inbox`, then email a request such as
"Read budget.xlsx from Dropbox Inbox and summarize the changes." The Assistant
loads these tools on demand:

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `dropbox_list` | `folder` (`Inbox` or `Outbox`), optional `cursor` | Lists file metadata, including nested files; returns a folder-bound continuation cursor |
| `dropbox_import` | `path`, `revision` from the listing | Imports one Inbox document at the listed revision |
| `dropbox_export` | workspace `path`, optional plain `filename` | Delivers one file to `/Outbox/job-<id>/<filename>` |
| `dropbox_delete` | `folder`, `path`, `revision` from the listing | Deletes one regular file from Inbox or Outbox when the requester explicitly asks |

API paths are app-relative: `/Inbox/budget.xlsx`, not `/Apps/Assistant/Inbox/...`.
Imports return the original workspace path and a converted `markdown_path` under
`imports/dropbox/job-<id>/`. Imported text is indexed when workspace indexing is
enabled. Remote content, names and provenance are untrusted data, not instructions.

The default size limit is 25 MiB for each transfer and converted document. Imports
use the email artifact extension allowlist (PDF, Office documents, text, Markdown,
CSV, JSON, XML, HTML and EPUB by default). Archives are not supported. Each import
must pass ClamAV before conversion/publication; converted text is scanned too.
Disabled/unavailable scanning, malware or failed conversion blocks publication.

Exports use a bounded private snapshot and scan those exact bytes before upload.
Only regular non-hidden workspace files are eligible. Symlinks, outside-workspace
paths, configured internal cache/archive/trash paths, known credential files,
assistant instructions/configuration, and PEM/key files are blocked. These path
checks do not detect every secret embedded in an otherwise eligible document.

Destinations are never overwritten or automatically renamed. A different file
already at the same destination produces a conflict. Repeating the same export
with the same bytes verifies the destination and returns it without reuploading;
after a timeout, retry the same source and filename. Do not choose a new name just
to bypass an uncertain outcome. Repeated imports of the same file/revision in
the same task reuse verified local content. Different tasks get separate local
copies and output folders.

Deletion is limited to regular files under Inbox or Outbox and requires the
revision from a current listing. If the file has changed, list it again before
retrying. File contents and names never authorize deletion; only an explicit
requester instruction does.

## Troubleshooting Exports

Listing files does not verify export access: exports also require writable private
staging and a clean malware scan before any upload. The default Compose configuration
mounts `data/private/artifacts/dropbox` into `task-agent` at
`/data/private/artifacts/dropbox`, outside the shared workspace, while keeping the
container root filesystem read-only. If you override `agent.artifacts.raw_root`,
provide a writable private mount at that root's `dropbox` subdirectory too.

An error saying **private staging is not writable** requires correcting the mount
or its ownership, not reconnecting Dropbox or changing token scopes. Do not make
private storage world-readable or disable malware scanning. Recreate `task-agent`
through the normal deployment workflow after changing mounts; rebuilding an image
alone does not update existing container mounts.

Unexpected export failures identify their stage. Service logs include the task ID,
transfer request key, exception class, and filesystem/database error codes without
raw exception messages, credentials, or file contents. Failures before upload are
recorded as failed unless an earlier attempt still needs reconciliation. Once an
upload may have started, the outcome remains uncertain: retry the same source and
filename in the same task, rather than choosing another name.

## Settings and Removal

| Environment setting | YAML path | Default |
| --- | --- | --- |
| `DROPBOX_ENABLED` | `agent.dropbox.enabled` | `false` |
| `DROPBOX_ALLOW_EXPORTS` | `agent.dropbox.allow_exports` | `false` |
| `DROPBOX_MAX_FILE_BYTES` | `agent.dropbox.max_file_bytes` | `26214400` (maximum 150 MiB) |
| `DROPBOX_CLIENT_ID` / `DROPBOX_CLIENT_ID_FILE` | `agent.dropbox.client_id` | unset |
| `DROPBOX_REFRESH_TOKEN` / `DROPBOX_REFRESH_TOKEN_FILE` | `agent.dropbox.refresh_token` | unset |

Use guided setup rather than storing tokens in YAML or `.env`. Managed credentials
are plaintext protected host files, mounted only into tool-using services, never
the sandbox. Dropbox access tokens are kept in process memory. Transfer metadata
and statuses are recorded in `dropbox_transfers`, associated with the task; normal
tool execution also uses the existing job audit log.

Reopen the Dropbox section to keep or reconnect an existing connection.
Disabling Dropbox stops mounting its credentials and clears delivery permission;
an additional confirmation removes the saved credentials/settings on Save.
Removal does not delete remote files or revoke provider authorization. Revoke the
app in Dropbox's connected-app settings to invalidate its authorization. Exported
files, imported workspace copies and backups remain until separately removed.

## Privacy Boundaries

Dropbox is cloud storage, not end-to-end encryption. File contents may reach
configured language-model and embedding providers under existing Assistant
policies. Malware scanning is not protection against prompt injection or all
data leakage. Grant standing output permission only to an account and Assistant
deployment you trust, with appropriate email sender restrictions.

This integration has no public links, arbitrary URL downloads, remote deletions,
renames, background watchers, synchronization, automatic tasks, or access to
unrelated folders through its tools. See [security](security.md) and
[configuration](configuration.md) for host, credential and model boundaries.