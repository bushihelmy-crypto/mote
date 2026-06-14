#!/usr/bin/env python
# -*- coding: utf-8 -*-
from pydantic import BaseModel, Field


class ServiceToServiceJWTConfig(BaseModel):
    token: str = Field(default="", description="JWT token")
    base_url: str = Field(default="", description="JWT base url")
