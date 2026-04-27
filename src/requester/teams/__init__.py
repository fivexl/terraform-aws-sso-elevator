# Bot Framework REST URLs: encode conversation/activity id path segments (microsoft-teams-api omits this).
from .teams_bot_framework_path_encoding import apply as _apply_bot_framework_path_encoding

_apply_bot_framework_path_encoding()
