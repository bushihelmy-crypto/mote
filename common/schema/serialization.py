from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_serializer, model_validator


class BaseSerialization(BaseModel, extra="forbid"):
    """Polymorphic subclass serialization / deserialization mixin.

    Pydantic is not designed for polymorphism: a subclass dumped through a base
    annotation loses its concrete type. This mixin appends a
    ``__module_class_name`` tag on dump and, on validate, looks the tag up in a
    subclass registry to reconstruct the concrete type.

    See https://docs.pydantic.dev/latest/concepts/serialization/.
    """

    __is_polymorphic_base = False
    __subclasses_map__ = {}

    @model_serializer(mode="wrap")
    def __serialize_with_class_type__(self, default_serializer) -> Any:
        # default serializer, then append the `__module_class_name` field and return
        ret = default_serializer(self)
        ret["__module_class_name"] = f"{self.__class__.__module__}.{self.__class__.__qualname__}"
        return ret

    @model_validator(mode="wrap")
    @classmethod
    def __convert_to_real_type__(cls, value: Any, handler):
        if isinstance(value, dict) is False:
            return handler(value)

        # it is a dict so make sure to remove the __module_class_name
        # because we don't allow extra keywords but want to ensure
        # e.g Cat.model_validate(cat.model_dump()) works
        class_full_name = value.pop("__module_class_name", None)

        # if it's not the polymorphic base we construct via default handler
        if not cls.__is_polymorphic_base:
            if class_full_name is None:
                return handler(value)
            elif str(cls) == f"<class '{class_full_name}'>":
                return handler(value)
            else:
                # f"Trying to instantiate {class_full_name} but this is not the polymorphic base class")
                pass

        # otherwise we lookup the correct polymorphic type and construct that
        # instead
        if class_full_name is None:
            raise ValueError("Missing __module_class_name field")

        class_type = cls.__subclasses_map__.get(class_full_name, None)

        if class_type is None:
            # TODO could try dynamic import
            raise TypeError(f"Trying to instantiate {class_full_name}, which has not yet been defined!")

        return class_type(**value)

    def __init_subclass__(cls, is_polymorphic_base: bool = False, **kwargs):
        cls.__is_polymorphic_base = is_polymorphic_base
        cls.__subclasses_map__[f"{cls.__module__}.{cls.__qualname__}"] = cls
        super().__init_subclass__(**kwargs)
