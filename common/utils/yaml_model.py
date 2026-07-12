#!/usr/bin/env python
# -*- coding: utf-8 -*-
from pathlib import Path
from typing import Dict, Optional

import yaml
from pydantic import BaseModel


class YamlModel(BaseModel):
    """Base class for yaml model"""

    extra_fields: Optional[Dict[str, str]] = None

    @classmethod
    def read_yaml(cls, file_path: Path, encoding: str = "utf-8") -> Dict:
        """Read yaml file and return a dict"""
        if not file_path.exists():
            return {}
        with open(file_path, "r", encoding=encoding) as file:
            return yaml.safe_load(file)

    @classmethod
    def from_yaml_file(cls, file_path: Path) -> "YamlModel":
        """Read yaml file and return a YamlModel instance"""
        return cls(**cls.read_yaml(file_path))

    def to_yaml_file(self, file_path: Path, encoding: str = "utf-8") -> None:
        """Dump YamlModel instance to yaml file"""
        with open(file_path, "w", encoding=encoding) as file:
            yaml.dump(self.model_dump(), file)
