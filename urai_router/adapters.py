"""Translations between the OpenAI chat format and Anthropic Messages.

Urai Router's public surface is OpenAI-compatible.  Providers that only speak
the Anthropic Messages API (for example some OpenCode Zen models) are adapted
here so every caller sees one format.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Generator, Iterable, List, Optional

from .util import first_text, sse_event

FINISH_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}


def _image_block(part: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if not url or not isinstance(url, str):
        return None
    if url.startswith("data:"):
        try:
            header, data = url.split(",", 1)
            media = header.split(";", 1)[0][len("data:") :] or "image/png"
        except ValueError:
            return None
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _content_blocks(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        text = first_text(content)
        return [{"type": "text", "text": text}] if text else []
    blocks: List[Dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part:
                blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = str(part.get("text", ""))
            if text:
                blocks.append({"type": "text", "text": text})
        elif ptype in ("image_url", "image"):
            image = _image_block(part)
            if image:
                blocks.append(image)
        elif ptype == "input_text":
            text = str(part.get("text", ""))
            if text:
                blocks.append({"type": "text", "text": text})
        elif "text" in part:
            text = str(part.get("text", ""))
            if text:
                blocks.append({"type": "text", "text": text})
    return blocks


def _merge_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for message in messages:
        if merged and merged[-1]["role"] == message["role"]:
            prev = merged[-1]
            prev_blocks = prev["content"] if isinstance(prev["content"], list) else []
            new_blocks = message["content"] if isinstance(message["content"], list) else []
            prev["content"] = prev_blocks + new_blocks
        else:
            merged.append(message)
    return merged


def openai_to_anthropic(body: Dict[str, Any]) -> Dict[str, Any]:
    messages: List[Dict[str, Any]] = []
    system_parts: List[str] = []

    for raw in body.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        content = raw.get("content")
        if role == "system":
            text = first_text(content)
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            result = first_text(content)
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": raw.get("tool_call_id", ""),
                            "content": result,
                        }
                    ],
                }
            )
            continue
        if role == "assistant":
            blocks: List[Dict[str, Any]] = []
            text = first_text(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for call in raw.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                arguments = fn.get("arguments") or "{}"
                if isinstance(arguments, str):
                    try:
                        parsed_args = json.loads(arguments)
                    except ValueError:
                        parsed_args = {"raw": arguments}
                else:
                    parsed_args = arguments
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or f"toolu_{len(blocks)}",
                        "name": fn.get("name") or "tool",
                        "input": parsed_args if isinstance(parsed_args, dict) else {"value": parsed_args},
                    }
                )
            messages.append({"role": "assistant", "content": blocks})
            continue
        blocks = _content_blocks(content)
        messages.append({"role": "user", "content": blocks})

    messages = [m for m in messages if m.get("content")]
    messages = _merge_messages(messages)
    if not messages:
        messages = [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]

    result: Dict[str, Any] = {
        "model": body.get("model"),
        "messages": messages,
        "max_tokens": int(body.get("max_tokens") or body.get("max_completion_tokens") or 4096),
    }
    if system_parts:
        result["system"] = "\n\n".join(system_parts)
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"), ("top_k", "top_k")):
        if body.get(src) is not None:
            result[dst] = body[src]
    if body.get("stop") is not None:
        stop = body["stop"]
        result["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    if body.get("stream"):
        result["stream"] = True

    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function") or {}
        tools.append(
            {
                "name": fn.get("name"),
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    if tools:
        result["tools"] = tools

    choice = body.get("tool_choice")
    if choice is not None:
        if choice == "auto":
            result["tool_choice"] = {"type": "auto"}
        elif choice == "required":
            result["tool_choice"] = {"type": "any"}
        elif choice == "none":
            result["tool_choice"] = {"type": "none"}
        elif isinstance(choice, dict):
            name = (choice.get("function") or {}).get("name")
            if name:
                result["tool_choice"] = {"type": "tool", "name": name}

    if body.get("metadata"):
        result["metadata"] = body["metadata"]
    return result


def anthropic_to_openai(data: Dict[str, Any], fallback_model: str = "") -> Dict[str, Any]:
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text", "")))
        elif btype in ("thinking", "redacted_thinking"):
            if block.get("thinking"):
                reasoning_parts.append(str(block["thinking"]))
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                }
            )
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
        if message["content"] is None:
            message["content"] = None

    usage_in = data.get("usage") or {}
    usage = {
        "prompt_tokens": int(usage_in.get("input_tokens") or 0),
        "completion_tokens": int(usage_in.get("output_tokens") or 0),
        "total_tokens": int(usage_in.get("input_tokens") or 0) + int(usage_in.get("output_tokens") or 0),
    }
    if usage_in.get("cache_read_input_tokens") or usage_in.get("cache_creation_input_tokens"):
        usage["prompt_tokens_details"] = {
            "cached_tokens": int(usage_in.get("cache_read_input_tokens") or 0),
            "cache_write_tokens": int(usage_in.get("cache_creation_input_tokens") or 0),
        }

    return {
        "id": data.get("id") or "chatcmpl-urai",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get("model") or fallback_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": FINISH_MAP.get(str(data.get("stop_reason") or ""), "stop"),
            }
        ],
        "usage": usage,
    }


def anthropic_stream_to_openai(
    events: Iterable[Dict[str, Any]], fallback_model: str = ""
) -> Generator[bytes, None, None]:
    created = int(time.time())
    message_id = "chatcmpl-urai"
    model = fallback_model
    sent_role = False
    tool_index = -1
    block_tool_index: Dict[int, int] = {}
    finish_reason = "stop"
    usage: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def chunk(delta: Dict[str, Any], finish: Optional[str] = None) -> bytes:
        payload = {
            "id": message_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return sse_event(payload)

    for event in events:
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            message_id = message.get("id") or message_id
            model = message.get("model") or model
            usage["prompt_tokens"] = int((message.get("usage") or {}).get("input_tokens") or 0)
            if not sent_role:
                yield chunk({"role": "assistant", "content": ""})
                sent_role = True
        elif etype == "content_block_start":
            block = event.get("content_block") or {}
            index = int(event.get("index") or 0)
            btype = block.get("type")
            if btype == "text" and not sent_role:
                yield chunk({"role": "assistant", "content": ""})
                sent_role = True
            elif btype == "tool_use":
                tool_index += 1
                block_tool_index[index] = tool_index
                if not sent_role:
                    yield chunk({"role": "assistant", "content": ""})
                    sent_role = True
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": tool_index,
                                "id": block.get("id"),
                                "type": "function",
                                "function": {"name": block.get("name"), "arguments": ""},
                            }
                        ]
                    }
                )
        elif etype == "content_block_delta":
            index = int(event.get("index") or 0)
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                yield chunk({"content": delta.get("text", "")})
            elif dtype == "thinking_delta":
                yield chunk({"reasoning_content": delta.get("thinking", "")})
            elif dtype == "input_json_delta":
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": block_tool_index.get(index, 0),
                                "function": {"arguments": delta.get("partial_json", "")},
                            }
                        ]
                    }
                )
        elif etype == "message_delta":
            delta = event.get("delta") or {}
            finish_reason = FINISH_MAP.get(str(delta.get("stop_reason") or ""), finish_reason)
            usage["completion_tokens"] = int((event.get("usage") or {}).get("output_tokens") or 0)
        elif etype == "message_stop":
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
            yield chunk({}, finish_reason)
            if usage["prompt_tokens"] or usage["completion_tokens"]:
                yield sse_event(
                    {
                        "id": message_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": usage,
                    }
                )
            yield sse_event("[DONE]")
            return
        elif etype == "error":
            error = event.get("error") or {}
            yield sse_event({"error": {"message": error.get("message", "anthropic stream error"), "type": "upstream_error"}})
            yield sse_event("[DONE]")
            return
    yield sse_event("[DONE]")
