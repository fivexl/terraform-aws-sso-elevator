import dataclasses
import enum
from typing import FrozenSet

from pydantic import BaseModel as PydanticBaseModel
from pydantic import ConfigDict


class BaseModel(PydanticBaseModel):
    model_config = ConfigDict(frozen=True)

    def dict(self, *args, **kwargs) -> dict:  # noqa: ANN101, ANN003, ANN002
        """Converts instance to dict representation of it. Workaround for https://github.com/pydantic/pydantic/issues/1090"""
        # In Pydantic v2, use model_dump instead
        result = self.model_dump(*args, **kwargs)
        # Convert frozensets to lists for JSON serialization
        for field_name in self.__class__.model_fields.keys():
            attr = getattr(self, field_name)
            if isinstance(attr, FrozenSet):
                result[field_name] = list(attr)
        return result

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
