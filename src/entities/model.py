import dataclasses
import enum
from typing import FrozenSet

from pydantic import BaseModel as PydanticBaseModel


class BaseModel(PydanticBaseModel):
    class Config:
        frozen = True

    def dict(self, *args, **kwargs) -> dict:  # noqa: ANN101, ANN003, ANN002
        """Converts instance to dict representation of it. Workaround for https://github.com/pydantic/pydantic/issues/1090"""
        cp = super().copy()
        cp.Config.frozen = False
        for field_name in cp.__fields__.keys():
            attr = cp.__getattribute__(field_name)
            if isinstance(attr, FrozenSet):
                cp.__setattr__(field_name, list(attr))

        cp.Config.frozen = True
        # frozendict.frozendict(?)
        return PydanticBaseModel.dict(cp, *args, **kwargs)


def json_default(o: object) -> str | dict:
    if isinstance(o, PydanticBaseModel):
        return o.dict()
    elif dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    elif isinstance(o, enum.Enum):
        return o.value
    return str(o)
