from pydantic import Field, model_validator

from metagpt.common.logs import logger
from metagpt.common.utils.yaml_model import YamlModel


class UseragentConfig(YamlModel):
    enable: bool = Field(default=False, description="Whether to use UserAgent.")

    @model_validator(mode="after")
    def initialize(self):
        if not self.enable:
            logger.info("UserAgent is not enabled.")
            return self
        logger.info("UserAgent is enabled.")
        return self
