# Documentation Index

This directory is the canonical documentation set for the Assistant project.

For a project overview, quick start, and feature introduction, see the [root README](../README.md).

If you are new here, read in this order:

1. [advanced-configuration.md](advanced-configuration.md) — models, limits, email, integrations, UI, and scaling
2. [architecture.md](architecture.md) — service topology, runtime boundaries, and data flow
3. [agent-loop.md](agent-loop.md) — job lifecycle and agent execution loop
4. [prompting.md](prompting.md) — prompt construction, context, and compaction
5. [tools-reference.md](tools-reference.md) — all agent tools, grouped by capability
6. [openrouter.md](openrouter.md) — OpenRouter integration and model/tool usage

Then use the domain-specific docs below.

## Core Guides

- [architecture.md](architecture.md) — service topology, runtime boundaries, and data flow
- [agent-loop.md](agent-loop.md) — job lifecycle and agent execution loop
- [prompting.md](prompting.md) — prompt construction, context, and compaction
- [tools-reference.md](tools-reference.md) — all agent tools, grouped by capability
- [extending-tools.md](extending-tools.md) — how to add a new tool safely
- [openrouter.md](openrouter.md) — OpenRouter integration and model/tool usage

## Data & Storage

- [data-model.md](data-model.md) — PostgreSQL schema and entity relationships
- [memory-system.md](memory-system.md) — Memory Steward, notes, embeddings, and workspace index

## Configuration and Deployment

- [advanced-configuration.md](advanced-configuration.md) — **start here** for models, limits, email, integrations, UI, and scaling
- [configuration.md](configuration.md) — full `.env` and `config/agent.yaml` reference
- [security.md](security.md) — threat model, hardening checklist, network binding, reverse-proxy examples, and responsible disclosure
- [docker-setup.md](docker-setup.md) — image builds, Compose topology, networks, and mounts
- [running-assistant.md](running-assistant.md) — local and server deployment guide
- [concurrent-job-processing.md](concurrent-job-processing.md) — task-agent scaling

## Runtime Infrastructure

- [sandbox.md](sandbox.md) — command execution sandbox architecture

## Email Pipeline

- [email-and-artifacts.md](email-and-artifacts.md) — IMAP ingestion, disclosure handling, ClamAV scanning, and MarkItDown artifact conversion

## UI & Contributor Orientation

- [visual-workspace-ui.md](visual-workspace-ui.md) — `/admin` and `/workspace` UI behavior
- [dashboard-and-api.md](dashboard-and-api.md) — dashboard capabilities and API endpoint map
- [codebase.md](codebase.md) — module map, test structure, and contribution workflow

