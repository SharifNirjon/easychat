"""Pure data models shared by the API and the CLI scripts. No settings, no I/O."""
import re

from pydantic import BaseModel, Field, HttpUrl, field_validator

SITE_KEY_PATTERN = r"^pk_[A-Za-z0-9_]{8,64}$"
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"
ORIGIN_PATTERN = r"^https?://[a-z0-9.-]+(:[0-9]{1,5})?$"   # scheme://host[:port], lowercase, no path


def normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


class Greeting(BaseModel):
    title: str = Field(max_length=80)
    headline: str = Field(max_length=120)   # words wrapped in *asterisks* render in the accent serif font
    subtitle: str = Field(max_length=300)


class Theme(BaseModel):
    primary: str = Field(pattern=HEX_COLOR)
    primary_dark: str = Field(pattern=HEX_COLOR)
    accent: str = Field(pattern=HEX_COLOR)


class TenantConfig(BaseModel):
    """Everything the widget may see about a tenant. Stored in tenants.config."""
    company_name: str = Field(max_length=120)
    bot_name: str = Field(max_length=40)
    greeting: Greeting
    disclosure: str = Field(max_length=400)
    teaser: str = Field(max_length=200)
    suggested_prompts: list[str] = Field(max_length=3)
    privacy_url: HttpUrl
    theme: Theme


class TenantLimits(BaseModel):
    """Free-plan defaults. A tenant's tenants.limits JSON overrides individual fields."""
    daily_tokens: int = Field(default=1_000_000, gt=0)
    monthly_conversations: int = Field(default=300, gt=0)
    conversation_user_turns: int = Field(default=40, gt=0)


class TenantSeed(BaseModel):
    """A tenant + site key definition file, as read by seed_tenant.py."""
    slug: str = Field(pattern=SLUG_PATTERN)
    name: str = Field(max_length=120)
    site_key: str = Field(pattern=SITE_KEY_PATTERN)
    allowed_origins: list[str] = Field(min_length=1)
    limits: TenantLimits = TenantLimits()
    config: TenantConfig

    @field_validator("allowed_origins")
    @classmethod
    def origins_are_bare(cls, origins: list[str]) -> list[str]:
        cleaned = [normalize_origin(o) for o in origins]
        for origin in cleaned:
            if not re.match(ORIGIN_PATTERN, origin):
                raise ValueError(f"not a bare origin (scheme://host[:port]): {origin!r}")
        return cleaned
