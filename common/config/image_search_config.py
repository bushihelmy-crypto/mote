from typing import List, Optional, Union

from pydantic import BaseModel


class ImageSearchConfig(BaseModel):
    api_type: str = ""
    # support multiple api keys, if api_key is a list, randomly choose one key per call
    api_key: Union[str, List[str]] = ""
    api_base: Optional[str] = None
