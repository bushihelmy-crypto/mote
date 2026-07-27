from typing import Dict, Optional

from pydantic import BaseModel


class ConfigModel(BaseModel):
    """Base for validated configuration data without persistence behavior."""

    extra_fields: Optional[Dict[str, str]] = None
