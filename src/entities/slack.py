from .model import BaseModel


class User(BaseModel):
    id: str
    email: str
    real_name: str
