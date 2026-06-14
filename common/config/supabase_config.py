import re
from typing import ClassVar, Set, Union
from uuid import uuid4

from pydantic import Field, model_validator

from metagpt.common.logs import logger
from metagpt.common.utils.yaml_model import YamlModel


class SupabaseConfig(YamlModel):
    INVALID_VALUES: ClassVar[Set[Union[str, None]]] = {
        "",
        None,
        "YOUR_PROJECT_URL",
        "YOUR_PROJECT_KEY",
        "YOUR_ACCESS_TOKEN",
    }

    enable: bool = Field(default=False, description="Whether to use Supabase as the backend service.")
    project_url: str = Field(default=None, description="The endpoint URL for connecting to specific Supabase project.")
    project_key: str = Field(default=None, description="The anon/public key used for client API authentication.")
    project_ref: str = Field(default=None, description="The unique reference ID of your Supabase project.")
    access_token: str = Field(default=None, description="The service role API key for Supabase management API.")
    management_base_url: str = Field(
        default="https://api.supabase.com/v1", description="The base URL for Supabase management API endpoints."
    )
    session_id: str = Field(
        default_factory=lambda: uuid4().hex[:5],
        description="A unique session identifier generated for each chat. Used for creating isolated database tables.",
    )

    @model_validator(mode="after")
    def initialize(self):
        if not self.enable:
            logger.info("Supabase is not enabled.")
            return self

        self._check_required_fields()
        self.project_ref = self.project_ref or self._extract_project_ref()
        logger.info("Supabase is enabled.")

        return self

    def _check_required_fields(self):
        for field_name in ["project_url", "project_key", "access_token"]:
            value = getattr(self, field_name)
            if value in self.INVALID_VALUES:
                raise ValueError(f"When Supabase is enabled, {field_name} is required, but not provided.")

    def _extract_project_ref(self) -> str:
        # extract project_ref from project_url, e.g. https://mcpkxegjwqjovrmegysk.supabase.co -> mcpkxegjwqjovrmegysk
        match = re.search(r"//([^\.]+)\.", self.project_url)

        if not match:
            raise ValueError(f"Failed to extract project_ref from project_url: {self.project_url}")

        return match.group(1)
