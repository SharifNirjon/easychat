"""Request bodies. Validation here is the first line of defence against bad input."""
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tenancy import SITE_KEY_PATTERN

MAX_MESSAGE_CHARS = 4000


class ChatRequest(BaseModel):
    """One visitor turn. There is no history and no role field: the server owns the history,
    so a client can neither send a "system" message nor invent earlier "assistant" turns."""

    model_config = ConfigDict(extra="forbid")   # unknown fields (e.g. "messages") -> 422

    site_key: str = Field(pattern=SITE_KEY_PATTERN)
    conversation_id: UUID | None = None
    # None = retry: answer the stored history again (its last message must be the visitor's).
    message: str | None = Field(default=None, min_length=1, max_length=MAX_MESSAGE_CHARS)

    @model_validator(mode="after")
    def retry_needs_a_conversation(self) -> "ChatRequest":
        if self.message is None and self.conversation_id is None:
            raise ValueError("message is required to start a conversation")
        if self.message is not None and not self.message.strip():
            raise ValueError("message must not be blank")
        return self
