"""Request bodies. Validation here is the first line of defence against bad input."""
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blocks import FieldName, FormName
from guards import clean_message
from tenancy import SITE_KEY_PATTERN

MAX_MESSAGE_CHARS = 4000
MAX_FORM_FIELDS = 6
MAX_FIELD_CHARS = 1000         # a textarea (the lead summary); emails and names are checked shorter
MAX_CHOICES = 8                # options ticked in one multi-choice field (blocks.ChoiceField)

FieldText = Annotated[str, Field(max_length=MAX_FIELD_CHARS)]
ChoiceIds = Annotated[list[Annotated[str, Field(max_length=32)]], Field(max_length=MAX_CHOICES)]


class FormIn(BaseModel):
    """A chat form the visitor submitted (blocks.FormBlock with `submit`): its name and values.
    A value is text, or the ids ticked in a multi-choice field. Whether they fit the form that
    was offered is checked by its handler (chat_forms.py), never trusted from here."""

    model_config = ConfigDict(extra="forbid")

    name: FormName
    values: Annotated[dict[FieldName, FieldText | ChoiceIds], Field(max_length=MAX_FORM_FIELDS)]

    @field_validator("values")
    @classmethod
    def strip_hidden_characters(cls, values: dict[str, str | list[str]]) -> dict[str, str | list[str]]:
        """Same cleaning as messages: no invisible or control characters."""
        return {name: [clean_message(v) for v in value] if isinstance(value, list) else clean_message(value)
                for name, value in values.items()}


class ChatRequest(BaseModel):
    """One visitor turn. There is no history and no role field: the server owns the history,
    so a client can neither send a "system" message nor invent earlier "assistant" turns."""

    model_config = ConfigDict(extra="forbid")   # unknown fields (e.g. "messages") -> 422

    site_key: str = Field(pattern=SITE_KEY_PATTERN)
    conversation_id: UUID | None = None
    # None = retry: answer the stored history again (its last message must be the visitor's).
    message: str | None = Field(default=None, min_length=1, max_length=MAX_MESSAGE_CHARS)
    # A submitted chat form instead of a message (order tracking, step 8; chat forms, step 9). Its
    # values are used for this turn only; the conversation stores a summary without them.
    form: FormIn | None = None

    @field_validator("message")
    @classmethod
    def strip_hidden_characters(cls, message: str | None) -> str | None:
        """Remove invisible/control characters that could hide instructions (guards.clean_message)."""
        return None if message is None else clean_message(message)

    @model_validator(mode="after")
    def retry_needs_a_conversation(self) -> "ChatRequest":
        """A retry (no message) must name its conversation; messages can't be blank."""
        if self.message is None and self.form is None and self.conversation_id is None:
            raise ValueError("message is required to start a conversation")
        if self.message is not None and not self.message.strip():
            raise ValueError("message must not be blank")
        if self.form is not None and (self.message is not None or self.conversation_id is None):
            raise ValueError("a form is sent on its own, in an existing conversation")
        return self
