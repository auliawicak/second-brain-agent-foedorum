"""Operation-object messaging (Phase 6 §6.10).

Every Phase 6 output travels to the owner as a single, structured
Telegram operation object — a ProposalMarkdown (blockquote + quotable
caption + Up/Down/Apply Nav buttons) or a TextMessage. This keeps
feedback deterministic and testable, and lets the owner approve or
reject each proposal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BasePayload:
    """Minimal base for operation objects (markdown/text payloads)."""

    kind: str = "markdown"
    content: str = ""
    caption_prefix: str = ""
    quote: str = ""
    data: dict = field(default_factory=dict)
    payload_type: str = "proposal"
    actions: list = field(default_factory=list)

    def as_message(self) -> dict:
        raise NotImplementedError


# Hard caps for LLM-generated content (kept small so a runaway model can't
# spam the owner or run away with tokens).
MAX_STATEMENT_CHARS = 400
MAX_QUOTE_CHARS = 400
MAX_SECTION_CHARS = 900
MAX_CONSOLIDATION_SECTIONS = 8
MAX_BLAME_ITEMS = 3


@dataclass(slots=True)
class ConsolidatedStatement:
    """A single preference/fact `sentence` inside a consolidation proposal."""

    sentence: str = ""
    fact: str = ""
    reason: str = ""
    applied: bool = False  # applied on generate (supersede/forget paths)
    section: str = "preferences"
    blamable: bool = False
    details: dict = field(default_factory=dict)


@dataclass(slots=True)
class ProposalMarkdown(BasePayload):
    """A Telegram message that carries a proposal with voting + Apply nav."""

    kind: str = "markdown"  # multimarkdown on Telegram; content holds the body
    content: str = ""  # the actual markdown body sent to the owner
    caption_prefix: str = ""  # prepended — e.g. the reason a proposal exists
    quote: str = ""  # quotable summary of what changed & why (owner-facing)
    data: dict = field(default_factory=dict)  # persisted with the vote
    payload_type: str = "proposal"
    # Rendered Actions (Telegram)
    actions: list = field(default_factory=lambda: [
        {"id": "propose-up", "label": "Likes", "value": "up"},
        {"id": "propose-down", "label": "Dislikes", "value": "down"},
        {"id": "apply", "label": "Apply", "value": "apply"},
    ])

    def _cap(self, value: str, limit: int) -> str:
        value = (value or "").strip()
        return value if len(value) <= limit else value[: limit - 1] + "…"

    def as_message(self) -> dict:
        """Render this payload into a ready-to-send Telegram dict.

        `sections` (list of {"title", "body", "type"}) drive the rendering;
        the blockquote + autocaption are composed by caller in
        OperationSender for a single message.
        """
        sections = self.data.get("sections", [])
        lines: list[str] = []
        for s in sections:
            title = (s.get("title") or "").strip()
            body = (s.get("body") or "").strip()
            if not body:
                continue
            if title:
                lines.append(f"**{title}**\n{body}".strip())
            else:
                lines.append(body)
        content = "\n\n".join(lines)
        content = self._cap(content, MAX_CONSOLIDATION_SECTIONS * MAX_SECTION_CHARS)
        return {
            "text": content,
            "actions": list(self.actions),  # caller binds nav to a real button
        }


@dataclass(slots=True)
class TextMessage(BasePayload):
    """A plain owner-facing confirmation/notification message."""

    kind: str = "text"
    content: str = ""
    caption_prefix: str = ""
    quote: str = ""
    data: dict = field(default_factory=dict)
    payload_type: str = "text"
    actions: list = field(default_factory=lambda: [])

    def as_message(self) -> dict:
        return {"text": self.content, "actions": []}


# ─── Sender bridge ─────────────────────────────────────────────────────────
# The Telegram bot injects a `proposal_sender` callable at startup; when it
# is absent (tests, headless) every proposal logs instead of sending.

_SENDER = None


def set_proposal_sender(sender) -> None:
    """Inject the async sender: (old_content, proposed, rationale) -> token."""
    global _SENDER
    _SENDER = sender


async def send_proposal(
    old_content: str,
    proposed: str,
    rationale: str,
    *,
    kind: str = "persona",
    meta: dict | None = None,
) -> str | None:
    """Send a Phase 6 interactive proposal to the owner.

    Returns the callback token (str) when the proposal was actually sent, or
    None when headless. The owner's Apply/Reject decision is later applied
    via `apply_persona_proposal`.
    """
    if _SENDER is None:
        logger.info("Proposal (headless, not sent) [%s]: %s", kind, proposed[:400])
        return None
    try:
        token = await _SENDER(old_content, proposed, rationale, meta=meta)
        return token
    except Exception:
        logger.exception("Failed to send proposal [%s].", kind)
        return None


# ─── Headless test double ───────────────────────────────────────────────────


class FakeProposalSender:
    """Records sent proposals in memory; used by tests."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self._n = 0

    async def __call__(
        self,
        old_content: str,
        proposed: str,
        rationale: str,
        *,
        meta: dict | None = None,
    ) -> str:
        self._n += 1
        token = f"fake-{self._n}"
        self.sent.append((old_content, proposed, rationale, meta))
        return token