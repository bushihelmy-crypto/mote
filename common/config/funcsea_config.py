from pydantic import Field

from metagpt.common.utils.yaml_model import YamlModel


class FuncseaConfig(YamlModel):
    """Configuration for FuncSea"""

    enable: bool = Field(default=False, description="Enable FuncSea")
    base_url: str = Field(default="", description="FuncSea internal API base URL (SDK entrypoint)")
    api_key: str = Field(default="", description="FuncSea API key (SDK entrypoint)")
    public_base_url: str = Field(
        default="",
        description="FuncSea public base URL for generated app (optional; defaults to base_url)",
    )
    user_id: str = Field(default="", description="Used with app_id to create the database_url")
    user_email: str = Field(default="", description="Used with user_id to create the admin user")
    app_id: str = Field(default="", description="Used with user_id to create the database_url")
    stripe_api_key: str = Field(default="", description="Used for Stripe payments")
    app_database_url: str = Field(default="", description="User app database_url")
    env: str = Field(default="dev", description="Used for env creation")
    dev_base_url: str = Field(default="", description="dev base url")
    preview_base_url: str = Field(default="", description="preview base url")
    ui_style: str = Field(default="default", description="UI style")
    app_ai_key: str = Field(default="", description="AI service API key for aihub module")
    app_ai_base_url: str = Field(default="", description="AI service base URL for aihub module")
