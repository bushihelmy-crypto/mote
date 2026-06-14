from pydantic import Field

from metagpt.common.utils.yaml_model import YamlModel


class FrontendEngineerConfig(YamlModel):
    enable_subagent: bool = Field(
        default=False,
        description="Whether FrontendEngineer can delegate bounded implementation work to a subagent.",
    )
