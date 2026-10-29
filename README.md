<p align="center">
  <img src="docs/assets/logo.png" alt="Assistant" width="160">
</p>

<h1 align="center">Assistant</h1>

<p align="center">
  A self-hosted AI assistant you use through email or Telegram.
</p>

Assistant can research a topic, work with files, run code, set reminders, and
manage calendars and meetings. It runs in Docker on your computer or server.
You choose the models and which integrations it can use.

> **V2 is a prerelease.** Keep backups and test it before relying on it for important work.
> Upgrading from V1? Follow the [transition guide](https://github.com/rb81/assistant/releases/tag/layout-transition) first.

## Before you install

Have these ready:

- **Docker with Docker Compose.** On macOS, start Docker Desktop and wait for it to be ready. On Linux, make sure your Docker daemon is running.
- **Python 3.12 or newer**, with virtual-environment support, plus **Git** and **OpenSSL**.
- **A model API key.** OpenRouter is the default; an OpenAI-compatible provider is also supported. API usage may cost money.
- **A way to message your assistant:** a dedicated email account with IMAP/SMTP access, a Telegram bot token, or both. For Telegram, you also need your numeric user ID.

Use a terminal at least **76 columns wide and 24 rows tall**. An internet
connection is needed to install dependencies, download images and models, and
contact your chosen providers.

> [!WARNING]
> **Use an assistant-owned mailbox, not your personal inbox.** Email intake can move
> messages between folders. Restrict who can send requests and which domains can
> receive replies. Starting the app may process messages already in that mailbox.

> [!IMPORTANT]
> Self-hosted does not mean fully local: messages, files, and other information
> may be sent to the model providers and integrations needed for a task.
> Assistant can send messages, change connected accounts, and execute code.
> Start with narrow permissions. The sandbox broker has privileged Docker access;
> read the [security guide](docs/security.md) before using it with sensitive data.

## Install and set up

Clone the repository and open the terminal interface:

```sh
git clone https://github.com/rb81/assistant.git
cd assistant
./run.sh
```

On the first launch, accept the prompts to create an isolated administration
environment and install its dependencies. You don't need to install Python
packages globally or copy `.env.example`.

The home screen walks you through setup:

1. **Identity and timezone:** name your assistant and enter your details.
2. **Models and API access:** choose a model, enter your API key, and select **Generate secure database password** for a fresh installation.
3. **Choose a channel:** configure Email or Telegram under **Connections**. Enable only the other integrations you need.
4. **Review assistant instructions:** read and adapt the example instructions, then choose **Keep draft**.
5. **Review & save:** resolve any listed issues, check your changes, and confirm saving.

Use **Tab / Shift-Tab** to move, **Enter** to open an action, and **Esc** to go
back. The **About** panel explains each focused option; press **F1** for full help.
Forms keep a draft as you move between sections. Nothing is written until
you confirm **Save configuration**, and saving does not start the app.

For Telegram, add your numeric user ID to the allowed-user list. Verified
recipient mappings use JSON, for example `{"you@example.com": "123456"}`.
If you want approvals through Telegram instead of email, set **Settings →
Advanced settings → Approvals and names → Approval delivery** to `telegram`
and map your approval email to your allowed Telegram ID.
See [Telegram setup](docs/telegram-and-voice.md) for details.

## Start using it

Open **Services → Start app** and confirm. The first start builds the images
and downloads required components, so it can take a while. Check service health
in **Services**, and open a component's **Recent logs** if something fails.

Send a simple request to your assistant's mailbox or private Telegram bot, such as:

> Summarize this document and list the questions I should ask about it.

Keep the computer or server and Docker running while you want Assistant available.
There is no browser dashboard; `./run.sh` is the local management interface.

## Everyday use

Run `./run.sh` whenever you want to manage the installation:

| Where | What you can do |
|---|---|
| **Services** | Check health, read logs, start or stop components |
| **Activity** | Browse tasks, memories, reminders, projects, and script permissions |
| **Connections** | Configure email, Telegram, calendars, Zoom, and Dropbox |
| **Settings** | Edit instructions, change models, manage credentials, and review advanced settings |

After changing settings, **Review & save**, then use **Services → Apply settings**
on each affected component. Use **Settings → Credentials → Activate pending** for
saved credential changes. Restarts and applying settings can interrupt active work.

To stop everything, choose **Services → Stop app**. Your saved data stays in place.
If the full-screen interface doesn't work in your terminal, use `./run.sh --plain`
for line-oriented menus.

## Your data and backups

Local settings, credentials, files, and database state live under the gitignored
`data/` directory. Credentials are protected files, not encrypted storage.
Back up this directory securely; stop the app before taking a filesystem backup
that needs to be consistent.

If you move credentials to the optional Docker volume, **back up that volume
separately**. A copy of `data/` alone will not contain those credentials.
Resetting the database permanently deletes its contents and makes no backup.
See [data locations and recovery](docs/running-assistant.md#data-locations).

## More help

The **[documentation index](docs/README.md)** has detailed configuration,
integration guides, troubleshooting, and implementation references.
For startup problems, begin with [Docker troubleshooting](docs/docker-setup.md#troubleshooting-local-administration).

[Apache 2.0 license](LICENSE)
