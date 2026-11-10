"""Request bodies. Validation here is the first line of defence against bad input."""
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from tenancy import SITE_ID_PATTERN

MAX_MESSAGE_CHARS = 4000


class Message(BaseModel):
    # Only "user" and "assistant" are accepted: the client can never send a "system"
    # message, so it cannot override the assistant's instructions. Anything else -> HTTP 422.
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ChatRequest(BaseModel):
    site_id: str = Field(pattern=SITE_ID_PATTERN)
    messages: list[Message] = Field(min_length=1)

    @field_validator("messages")
    @classmethod
    def last_message_is_from_user(cls, messages: list[Message]) -> list[Message]:
        # The model should always be answering the visitor, not continuing itself.
        if messages[-1].role != "user":
            raise ValueError("last message must be from the user")
        return messages
