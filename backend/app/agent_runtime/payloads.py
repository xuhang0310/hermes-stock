from __future__ import annotations

import base64
from typing import Any


def user_text_from_content(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    for block in content:
        if block.get("type") == "text":
            return str(block.get("text") or "")
    return ""


def _load_image(user_id: int, attachment: dict[str, Any]) -> tuple[bytes, str, str]:
    from backend.app.skills.agent_workspace import _resolve_in_jail, workspace_user_scope

    path = str(attachment.get("path") or "")
    with workspace_user_scope(user_id):
        target = _resolve_in_jail(path)
    if not target.exists() or not target.is_file() or target.is_symlink():
        raise FileNotFoundError(f"Agent attachment no longer exists: {path}")
    return (
        target.read_bytes(),
        str(attachment.get("mime_type") or "application/octet-stream"),
        str(attachment.get("name") or target.name),
    )


def materialize_openai_content(request_payload: dict[str, Any]) -> str | list[dict[str, Any]]:
    text = str(request_payload.get("user_text") or request_payload.get("question") or "")
    image_parts: list[dict[str, Any]] = []
    for attachment in request_payload.get("attachments") or []:
        if attachment.get("kind") != "image":
            continue
        raw, media_type, _name = _load_image(int(request_payload["user_id"]), attachment)
        encoded = base64.b64encode(raw).decode("ascii")
        image_parts.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{encoded}"}})
    return [{"type": "text", "text": text}, *image_parts] if image_parts else text


def materialize_agentscope_content(request_payload: dict[str, Any]):
    from agentscope.message import Base64Source, DataBlock, TextBlock

    blocks: list[Any] = [TextBlock(text=str(request_payload.get("user_text") or request_payload.get("question") or ""))]
    for attachment in request_payload.get("attachments") or []:
        if attachment.get("kind") != "image":
            continue
        raw, media_type, name = _load_image(int(request_payload["user_id"]), attachment)
        blocks.append(
            DataBlock(
                source=Base64Source(data=base64.b64encode(raw).decode("ascii"), media_type=media_type),
                name=name,
            )
        )
    return blocks
