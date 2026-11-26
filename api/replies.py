"""Shapes shared by the parts of the chat that answer with code instead of the model:
order tracking (step 8) and the chat forms (step 9).

A submitted form's values are untrusted: a value may be text or a list (multi-choice fields),
whatever the form said, so handlers read them through text_value() / list_value(), which never
raise on the wrong shape.
"""
from dataclasses import dataclass

FormValue = str | list[str]


@dataclass(frozen=True)
class FormSubmission:
    """A submitted chat form (schemas.FormIn): which form, and its field values."""
    name: str
    values: dict[str, FormValue]


@dataclass(frozen=True)
class ScriptedReply:
    """A reply written by code instead of the model."""
    text: str
    blocks: list[dict]
    stored_text: str | None = None     # kept in the conversation instead of `text`
    store_blocks: bool = True          # False: the blocks hold personal or order data, so they aren't stored
    forget: bool = False               # the conversation was deleted ("delete my data"): store nothing


def text_value(values: dict[str, FormValue], name: str) -> str:
    """A text field's value, trimmed; '' if it is missing or not text."""
    value = values.get(name)
    return value.strip() if isinstance(value, str) else ""


def list_value(values: dict[str, FormValue], name: str) -> list[str]:
    """A multi-choice field's values (a single text counts as a one-item list), without duplicates."""
    value = values.get(name)
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return list(dict.fromkeys(item for item in items if isinstance(item, str) and item))
