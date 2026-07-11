from typing import Any

from pydantic import Field

from mote.common.utils.yaml_model import YamlModel


class SentryConfig(YamlModel):
    """Configuration for Sentry error tracking and monitoring"""

    enable: bool = Field(default=False, description="Enable Sentry error tracking")
    dsn: str = Field(default="", description="Sentry DSN endpoint")
    environment: str = Field(default="algo-debug", description="Application environment tag")
    send_default_pii: bool = Field(default=True, description="Whether to collect user info such as IP addresses")

    extra_data: dict[str, Any] = Field(
        default_factory=dict, description="Extra data to include in Sentry events (e.g., user_id, chat_id, etc.)"
    )
