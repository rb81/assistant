import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Iterator, Optional

from .config import AppConfig, app_display_name, app_referer_url
from .llm_client import LlmClient
from .prompt_context import load_agent_prompt


LOGGER = logging.getLogger("assistant.chat_responder")

QUICK_CHAT_INSTRUCTIONS = """You are in quick chat mode -- a fast, casual conversational lane, not the full task-agent pipeline.
- Keep replies concise and conversational.
- You have no tools and no memory access in this mode.
- Call escalate_to_job for anything that requires tools, data access, file changes, sending email, or work that will take more than a few seconds.
- If the session already has a job in progress, do not escalate again -- tell the user it's still working."""

ESCALATE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "escalate_to_job",
        "description": "Hand this request to the full task-agent pipeline for anything requiring tools, data access, file changes, email, or longer work.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_summary": {
                    "type": "string",
                    "description": "A concise summary of the task, written for the agent that will pick it up.",
                },
            },
            "required": ["task_summary"],
        },
    },
}


def build_messages(config: AppConfig, history: list[dict[str, Any]], user_message: str) -> list[dict[str, Any]]:
    persona = load_agent_prompt(config, max_bytes=4000)
    system_content = "%s\n\n%s" % (persona, QUICK_CHAT_INSTRUCTIONS)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    for row in history:
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        role = row.get("role") if row.get("role") in ("user", "assistant") else "assistant"
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    return messages


def condense_transcript(history: list[dict[str, Any]], max_turns: int = 10, max_chars: int = 4000) -> str:
    recent = history[-max_turns:] if max_turns > 0 else history
    lines = []
    for row in recent:
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        label = "User" if row.get("role") == "user" else "Arqis"
        lines.append("%s: %s" % (label, content))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text


def _api_key(base_url: str) -> str:
    if "openrouter.ai" in base_url.lower():
        key = os.getenv("OPENROUTER_API_KEY")
    else:
        key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("model API key is required for chat_responder")
    return key


def _stream_request(
    config: AppConfig,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    timeout_seconds: int,
):
    base_url = str(config.get("agent.llm.base_url", "https://openrouter.ai/api/v1")).rstrip("/")
    api_key = _api_key(base_url)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "tools": [ESCALATE_TOOL],
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    body = json.dumps(payload, default=str).encode("utf-8")
    request = urllib.request.Request(
        "%s/chat/completions" % base_url,
        data=body,
        headers={
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "application/json",
            "HTTP-Referer": app_referer_url(config),
            "X-Title": app_display_name(config),
        },
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=timeout_seconds)


def parse_sse_lines(lines: Iterator[bytes]) -> Iterator[dict[str, Any]]:
    """Decode an OpenAI-compatible SSE byte-line stream into JSON chunk dicts."""
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def _consume_stream(chunks: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Pure state machine: parsed stream chunks -> reply events. No I/O."""
    tool_call_name: Optional[str] = None
    tool_call_args = ""
    finish_reason: Optional[str] = None
    usage: Optional[dict[str, Any]] = None

    for chunk in chunks:
        if chunk.get("usage"):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            yield {"type": "delta", "text": delta["content"]}
        for tool_call in delta.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            if function.get("name"):
                tool_call_name = function["name"]
            if function.get("arguments"):
                tool_call_args += function["arguments"]
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

    if finish_reason == "tool_calls" and tool_call_name == "escalate_to_job":
        try:
            args = json.loads(tool_call_args or "{}")
        except json.JSONDecodeError:
            args = {}
        yield {"type": "escalated", "task_summary": str(args.get("task_summary") or "").strip()}
        return

    yield {"type": "done", "usage": usage}


def _fallback_completion(
    config: AppConfig,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
) -> Iterator[dict[str, Any]]:
    client = LlmClient(config, model=model, temperature=temperature, max_tokens=max_tokens)
    try:
        response = client.chat(messages, tools=[ESCALATE_TOOL])
    except Exception as exc:
        yield {"type": "error", "message": str(exc)}
        return
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        function = tool_calls[0].get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        yield {"type": "escalated", "task_summary": str(args.get("task_summary") or "").strip()}
        return
    content = str(message.get("content") or "")
    if content:
        yield {"type": "delta", "text": content}
    yield {"type": "done", "usage": response.get("usage")}


def generate_reply_events(
    config: AppConfig,
    history: list[dict[str, Any]],
    user_message: str,
) -> Iterator[dict[str, Any]]:
    """Turn (session history + new user message) into a stream of reply events."""
    model = str(config.get("agent.chat.model") or config.get("agent.llm.model", "openai/gpt-4.1"))
    temperature = config.get_float("agent.llm.temperature", 0.2)
    max_tokens = config.get_int("agent.llm.max_tokens_per_call", 4096)
    messages = build_messages(config, history, user_message)

    try:
        response = _stream_request(config, model, messages, max_tokens, temperature, timeout_seconds=60)
    except Exception as exc:
        LOGGER.warning("chat stream setup failed, falling back to non-streamed completion: %s", exc)
        yield from _fallback_completion(config, model, messages, max_tokens, temperature)
        return

    try:
        with response:
            yield from _consume_stream(parse_sse_lines(response))
    except Exception as exc:
        yield {"type": "error", "message": str(exc)}
