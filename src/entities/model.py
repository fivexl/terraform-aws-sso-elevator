import dataclasses
import enum

from pydantic import BaseModel as PydanticBaseModel
from pydantic import ConfigDict


def _convert_to_serializable(obj):  # noqa: ANN001, ANN202, PLR0911
    """Recursively convert objects to JSON-serializable format."""
    if isinstance(obj, PydanticBaseModel):
        # Manually extract fields to avoid Pydantic serialization issues with frozen models
        result = {}
        for field_name in obj.__class__.model_fields.keys():
            result[field_name] = _convert_to_serializable(getattr(obj, field_name))
        return result
    if isinstance(obj, (frozenset, set)):
        return [_convert_to_serializable(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _convert_to_serializable(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_convert_to_serializable(item) for item in obj]
    if isinstance(obj, enum.Enum):
        return obj.value
    return obj


class BaseModel(PydanticBaseModel):
    model_config = ConfigDict(frozen=True)

    def dict(self, *args, **kwargs) -> dict:  # noqa: ANN101, ANN003, ANN002, ARG002
        """Converts instance to dict representation of it. Workaround for https://github.com/pydantic/pydantic/issues/1090"""
        # Manually convert to avoid Pydantic serialization issues with frozensets of frozen models
        return _convert_to_serializable(self)

    def json(self, *args, **kwargs) -> str:  # noqa: ANN101, ANN003, ANN002
        """Converts instance to JSON string. Compatibility wrapper for Pydantic v2."""
        # In Pydantic v2, use model_dump_json instead
        return self.model_dump_json(*args, **kwargs)


def json_default(o: object) -> str | dict:
    if isinstance(o, PydanticBaseModel):
        return o.dict()
    elif dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    elif isinstance(o, enum.Enum):
        return o.value
    return str(o)
