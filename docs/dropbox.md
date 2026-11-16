# Dropbox File Exchange

Dropbox is an on-demand file exchange, not synchronization. A connected account
provides documents from its app `Inbox`; tasks can send files to
`Outbox/job-<id>` when standing export permission is enabled. Adding a file does
not start a task—ask the Assistant to use it through a connected channel.

## Connect

For a self-hosted installation, register an app in the
[Dropbox App Console](https://www.dropbox.com/developers/apps). Choose **App
Folder**, not **Full Dropbox**, and enable only `files.metadata.read`,
`files.content.read`, and `files.content.write`. App Folder is a registration
type, not an OAuth scope. Although the API client confines operations to Inbox
and Outbox, it cannot verify that a token belongs to an App Folder app.

For a shared application, its maintainer registers the app and provides the app
key; each user authorizes their own account. No client secret is needed.

Run:

```bash
./run.sh --section dropbox
```

Enable Dropbox and enter the app key and an **existing offline refresh token**
for that app. The current management form accepts credentials; it does not
display an OAuth consent URL or exchange an authorization code. Obtain the token
using Dropbox's [OAuth code flow with PKCE](https://developers.dropbox.com/oauth-guide),
requesting offline access (`token_access_type=offline`) and the scopes above.
Do not paste a short-lived access token into the refresh-token field.

Access tokens refresh automatically once connected. No public callback port or
host Dropbox client is required for the runtime integration. Keep authorization
codes, PKCE verifiers, and refresh tokens private.

Create `Inbox` under `/Apps/<your-app-name>` in Dropbox before importing.
`Outbox` and its task folder are created on first export. After saving and applying
settings, ask for a read-only Inbox listing to verify access.

Decide whether **any task may deliver files to Outbox without further
confirmation**. This is standing permission (off by default), not approval for
each upload. Review and save the connection, then recreate affected services.
Discarding a draft does not revoke authorization already granted at Dropbox.

## Use

Put a document in `/Apps/<your-app-name>/Inbox`, then ask the Assistant to use
it, for example: “Read budget.xlsx from Dropbox Inbox and summarize the changes.”

| Tool | Required input | What it does |
|---|---|---|
| `dropbox_list` | `folder` (`Inbox` or `Outbox`); optional `cursor` | Lists nested file metadata and returns a folder-bound continuation cursor |
| `dropbox_import` | `path` and `revision` from listing | Imports one Inbox document at the listed revision |
| `dropbox_export` | Workspace `path`; optional plain `filename` | Sends one file to `/Outbox/job-<id>/<filename>` |
| `dropbox_delete` | `folder`, `path`, and listed `revision` | Deletes one regular file when the requester explicitly asks |

Use app-relative paths such as `/Inbox/budget.xlsx`, not
`/Apps/Assistant/Inbox/...`. Imports return the original workspace path and a
converted `markdown_path` under `imports/dropbox/job-<id>/`. Imported text is
indexed when workspace indexing is enabled. File content and names are
untrusted data, never instructions or authority.

The default size limit is 25 MiB per transfer and converted document (configurable
up to 150 MiB). Imports use the email artifact extension allowlist; archives
are unsupported. Every import must pass ClamAV before conversion/publication,
and converted text is scanned too. Disabled/unavailable scanning, malware, or
failed conversion blocks publication.

Exports snapshot eligible workspace files into private staging and scan those
exact bytes before upload. Only regular, non-hidden files are eligible.
Symlinks, paths outside the workspace, internal cache/archive/trash locations,
known credential files, assistant instructions/configuration, and PEM/key files
are blocked. These checks cannot identify every secret embedded in an otherwise
eligible document.

Exports never overwrite or automatically rename. An existing different file at
the destination is a conflict. Retrying the same export with the same bytes
verifies an existing destination; after a timeout, retry the same source and
filename in the same task. Never change the name to bypass an uncertain result.
Repeated imports of the same file/revision within a task reuse verified content;
other tasks get separate copies and output folders.

Deletion is limited to regular files in Inbox or Outbox and requires the current
revision. If the file changed, list it again before retrying. Only an explicit
requester instruction authorizes deletion.

## Troubleshooting Exports

Export requires writable private staging and a clean malware scan, not just
Dropbox listing access. The default Compose setup mounts
`data/private/artifacts/dropbox` at `/data/private/artifacts/dropbox` in
`task-agent`, outside the shared workspace. If you override
`agent.artifacts.raw_root`, also provide a writable private mount at its
`dropbox` subdirectory.

For **private staging is not writable**, fix the mount or ownership; do not
reconnect Dropbox or widen permissions. Do not make private storage
world-readable or disable scanning. Recreate `task-agent` after mount changes;
an image rebuild does not change existing mounts.

Errors identify the failed stage. Logs record task ID, request key, exception
class, and filesystem/database error codes—not raw exception messages,
credentials, or file contents. A failure before upload is recorded as failed;
if an upload may have started, the result is uncertain. Retry the same source
and filename to reconcile it.

## Settings and Removal

| Environment setting | YAML path | Default |
|---|---|---|
| `AGENT_DROPBOX_ENABLED` | `agent.dropbox.enabled` | `false` |
| `AGENT_DROPBOX_ALLOW_EXPORTS` | `agent.dropbox.allow_exports` | `false` |
| `AGENT_DROPBOX_MAX_FILE_BYTES` | `agent.dropbox.max_file_bytes` | `26214400` (maximum 150 MiB) |
| `AGENT_DROPBOX_CLIENT_ID` / `_FILE` | `agent.dropbox.client_id` | unset |
| `AGENT_DROPBOX_REFRESH_TOKEN` / `_FILE` | `agent.dropbox.refresh_token` | unset |

Use setup rather than putting tokens in YAML or `.env`. Credentials are
protected plaintext host files, mounted only into tool-using services and never
the sandbox. Access tokens stay in process memory. Transfer status is recorded
in `dropbox_transfers` and the normal task audit log.

Reopen the Dropbox section to change credentials or disable the integration.
Disabling it and applying settings removes its runtime credential grants; it
does not automatically erase stored credentials. Remove those separately under
**Settings → Credentials** if no longer needed. Neither action removes
remote files or revokes provider authorization: revoke the app in Dropbox.
Imported workspace copies, exports, and backups also remain until removed
separately.

## Privacy Boundaries

Dropbox is cloud storage, not end-to-end encryption. File contents may be sent
to configured language-model and embedding providers under existing Assistant
policies. Malware scanning does not prevent prompt injection or all data
leakage. Enable standing export only for an account and deployment you trust,
with appropriate email sender restrictions.

The integration has no public links, arbitrary URL downloads, renames,
background watchers, synchronization, or automatic task creation. Its tools
cannot access unrelated Dropbox folders. Remote deletion is available only
through revision-checked `dropbox_delete`. See [security](security.md) and
[configuration](configuration.md).
