#!/usr/bin/env python3
"""Minimal Claude → OpenAI (APIPod) proxy.

Claude CLI 通过 ANTHROPIC_BASE_URL 指向本服务，本服务将
Anthropic Messages API 格式转为 OpenAI Chat Completions 格式，
转发到 APIPod，再转回 Anthropic 格式返回。
"""

import json
import os
import time
import uuid

import requests
from flask import Flask, jsonify, request

# ── 配置 ──────────────────────────────────────────────
# 密钥通过环境变量提供，切勿硬编码提交：
#   export APIPOD_API_KEY="your-key-here"
APIPOD_API_KEY = os.environ.get("APIPOD_API_KEY", "")
APIPOD_BASE_URL = os.environ.get("APIPOD_BASE_URL", "https://api.apipod.ai/v1")
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 3001

app = Flask(__name__)


# ── 工具函数 ──────────────────────────────────────────

def _anthropic_content_to_str(content) -> str:
    """将 Anthropic content 数组转成单字符串；过滤空块。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content else ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text", "")
        if text and text.strip():
            parts.append(text.strip())
    return "\n".join(parts) if parts else ""


def _build_openai_messages(anthropic_body: dict) -> list[dict]:
    """将 Anthropic 请求体转成 OpenAI messages 列表。"""
    messages = []

    # 处理 system prompt → OpenAI 第一条 system 消息
    system = anthropic_body.get("system")
    if system:
        system_text = _anthropic_content_to_str(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})

    # 处理 messages
    for msg in anthropic_body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Anthropic 的 assistant 消息可能包含 tool_use / tool_result，我们跳过
        if role in ("user", "assistant"):
            text = _anthropic_content_to_str(content)
            if text:
                messages.append({"role": role, "content": text})
            continue

        # tool_result → user 消息（简化处理，因为不需要 tool use）
        if role == "tool_result":
            text = _anthropic_content_to_str(content)
            if text:
                messages.append({"role": "user", "content": f"[tool_result]\n{text}"})
            continue

    return messages


def _build_openai_body(anthropic_body: dict) -> dict:
    """构建 OpenAI chat.completions 请求体。"""
    openai_messages = _build_openai_messages(anthropic_body)
    if not openai_messages:
        openai_messages = [{"role": "user", "content": "hello"}]

    body = {
        "model": anthropic_body.get("model", "claude-sonnet-4-6"),
        "messages": openai_messages,
        "max_tokens": anthropic_body.get("max_tokens", 4096),
    }

    # stop_sequences → stop
    stop = anthropic_body.get("stop_sequences")
    if stop:
        body["stop"] = stop

    return body


def _build_anthropic_response(openai_resp: dict, model: str) -> dict:
    """将 OpenAI 响应转为 Anthropic Messages 格式。"""
    choice = openai_resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    content_text = message.get("content", "") or ""
    stop_reason = choice.get("finish_reason", "end_turn")

    # 映射 finish_reason
    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "end_turn",
    }
    anthropic_stop = stop_reason_map.get(stop_reason, "end_turn")

    usage = openai_resp.get("usage", {})
    anthropic_usage = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": content_text}],
        "model": model,
        "stop_reason": anthropic_stop,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


# ── 路由 ──────────────────────────────────────────────

@app.route("/v1/models", methods=["GET"])
def list_models():
    """返回假模型列表（绕过 CLI 检查）。"""
    return jsonify({
        "data": [
            {
                "id": "claude-sonnet-4-6",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "apipod",
            },
            {
                "id": "claude-opus-4-7",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "apipod",
            },
        ],
        "object": "list",
    })


@app.route("/v1/whoami", methods=["GET"])
def whoami():
    """返回假用户信息（绕过 CLI 登录检查）。"""
    return jsonify({
        "id": "user_apipod",
        "name": "APIPod User",
        "email": "user@apipod.ai",
        "plan": "pro",
        "organization_id": "org_apipod",
    })


@app.route("/v1/messages", methods=["POST"])
def messages():
    """核心转换路由：Anthropic → OpenAI → APIPod → Anthropic。"""
    try:
        anthropic_body = request.get_json(force=True)
    except Exception as e:
        return jsonify({"error": {"message": f"invalid json: {e}"}}), 400

    if not anthropic_body:
        return jsonify({"error": {"message": "empty request body"}}), 400

    model = anthropic_body.get("model", "claude-sonnet-4-6")

    # 构建 OpenAI 请求
    openai_body = _build_openai_body(anthropic_body)

    # 如果所有 content 都被过滤成空了，返回错误
    if not any(
        msg.get("content", "").strip()
        for msg in openai_body.get("messages", [])
    ):
        return jsonify({
            "content": [{"type": "text", "text": "(empty request)"}],
            "model": model,
            "role": "assistant",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }), 200

    # 转发到 APIPod
    headers = {
        "Authorization": f"Bearer {APIPOD_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            f"{APIPOD_BASE_URL}/chat/completions",
            headers=headers,
            json=openai_body,
            timeout=120,
        )
    except requests.exceptions.Timeout:
        return jsonify({"error": {"message": "upstream timeout"}}), 504
    except requests.exceptions.ConnectionError as e:
        return jsonify({"error": {"message": f"upstream connection failed: {e}"}}), 502
    except requests.exceptions.RequestException as e:
        return jsonify({"error": {"message": f"upstream error: {e}"}}), 502

    # APIPod 返回非 200
    if resp.status_code != 200:
        try:
            error_body = resp.json()
        except Exception:
            error_body = {"message": resp.text[:500]}
        return jsonify({
            "error": {
                "message": error_body.get("error", {}).get("message", resp.text[:500])
            }
        }), resp.status_code

    # 转换响应
    try:
        openai_resp = resp.json()
    except Exception as e:
        return jsonify({"error": {"message": f"invalid upstream response: {e}"}}), 502

    anthropic_resp = _build_anthropic_response(openai_resp, model)
    return jsonify(anthropic_resp), 200


# ── 入口 ──────────────────────────────────────────────

if __name__ == "__main__":
    print(f"  Claude ↔ APIPod proxy running on http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"  Forwarding to: {APIPOD_BASE_URL}")
    print()
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False)
