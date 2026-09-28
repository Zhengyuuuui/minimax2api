"""Turning an OpenAI conversation into the one string the agent expects.

The upstream's session endpoint takes a single ``content`` field — it is an agent,
not a chat model, and it receives the whole conversation as one block of text the
way the web front-end sends it.  This module is the translation.

Three decisions are worth their own words:

- **System steps are inlined at the front.**  OpenAI's surface has a distinct
  ``system`` role; the upstream has no mapping for one.  It is written into the
  prompt as ``[系统指令] …`` rather than dropped, so a caller that relies on a
  system prompt still gets it — but it is deliberately *not* numbered, because
  numbering it would describe an instruction as one of the conversation's turns.
- **History is numbered; the final turn is not.**  The agent sees the turn order
  otherwise, and numbering an odd N steps keeps it from renumbering.  A request
  whose only message is a question is sent bare, because that is what the web
  client sends.
- **Images keep the shape they arrived in.**  An image part is passed through as
  the URL it names, last in the block.  Nothing is downloaded, decoded or
  re-uploaded here — see the gateway, which is where the upstream's own pipeline
  resolves them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# The role tags the agent sees.  Chinese because that is what the web front-end
# sends, and the agent is noticeably more reliable at following the numbering
# when the tags match what it was trained on.
_SYSTEM_TAG = "[系统指令]"
_ASSISTANT_TAG = "助手："
_USER_TAG = " 用户："

# A turn whose content is only images still has to say something: the agent
# refuses an empty string, and “看图” describes what the attachments are for
# without inventing a question the caller never asked.
_TEXT_ONLY_NOTE = "看图"


@dataclass
class Part:
    """One piece of a content array."""

    type: str = "text"
    text: str = ""
    image_url: str = ""


@dataclass
class Turn:
    """One OpenAI message."""

    role: str = "user"
    content: Any = ""
    images: list[str] = field(default_factory=list)

    def text(self) -> str:
        """The message's text, flattened from whichever shape it arrived in."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            chunks: list[str] = []
            for item in self.content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
            return "".join(chunks)
        return ""

    def image_urls(self) -> list[str]:
        """The URLs of any images in this message."""
        if self.images:
            return list(self.images)
        if isinstance(self.content, list):
            out: list[str] = []
            for item in self.content:
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                if kind in ("image_url", "input_image"):
                    node = item.get("image_url")
                    if isinstance(node, dict):
                        url = node.get("url")
                    else:
                        url = node
                    if isinstance(url, str) and url:
                        out.append(url)
            return out
        return []


def parse_turn(item: Any) -> Turn | None:
    """Read one OpenAI message, tolerating the shapes clients actually send.

    A missing or unknown role is treated as a user turn: an unlabelled message is
    far more often a client that left the role off than a system prompt that lost
    its tag.
    """
    if isinstance(item, str):
        return Turn(role="user", content=item)
    if not isinstance(item, dict):
        return None
    role = item.get("role")
    role = role.strip() if isinstance(role, str) else ""
    if role not in ("system", "assistant", "user", "tool"):
        role = "user"
    # Some clients put attachments in their own field rather than in the content
    # parts; both spellings are read rather than one being silently dropped.
    images = [
        url
        for url in (
            item.get("images") if isinstance(item.get("images"), list) else []
        )
        if isinstance(url, str) and url
    ]
    return Turn(role=role, content=item.get("content", ""), images=images)


def build_prompt(messages: Iterable[Turn]) -> str:
    """Flatten a conversation into the single string the agent reads.

    The final turn keeps its images last: the agent resolves attachments it is
    shown by position, and a note attached after the question is seen as a
    follow-up rather than a description of the thing being asked about.
    """
    turns = list(messages)
    if not turns:
        return ""

    parts: list[str] = []
    number = 0
    for turn in turns:
        text = turn.text().strip()
        images = turn.image_urls()
        if turn.role == "system":
            block = f"{_SYSTEM_TAG} {text}" if text else ""
        elif turn.role == "assistant":
            block = f"{_ASSISTANT_TAG}{text}" if text else ""
        else:
            number += 1
            prefix = _user_tag_for(number, len(turns))
            body = text or (_TEXT_ONLY_NOTE if images else "")
            block = f"{prefix}{body}" if body else ""
        if images:
            block = "\n".join([block] + [f"![已忽略的图片 {n}]({url})" for n, url in enumerate(images)])
        if block:
            parts.append(block)

    return "\n\n".join(parts)


def _user_tag_for(number: int, total: int) -> str:
    """The tag a user turn is written with.

    A single message carries no tag, because the web front-end sends a lone
    question bare; a tag there reads as evidence the caller mis-formed the
    request.  Multi-turn history is numbered so the agent keeps the order it was
    given.
    """
    if total == 1:
        return ""
    return f"{_USER_TAG}{number}. "
