"""Product-owned model input syntax and layered merge."""

from mote.product.config.model.inputs import (
    ExplicitModelsConfig,
    ProductModelsConfig,
    ShortcutModelsConfig,
    parse_product_models_config,
)
from mote.product.config.model.merge import ModelMergeResult, merge_product_model_layers

__all__ = [
    "ExplicitModelsConfig",
    "ModelMergeResult",
    "ProductModelsConfig",
    "ShortcutModelsConfig",
    "merge_product_model_layers",
    "parse_product_models_config",
]
