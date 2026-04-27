"""Bot Framework REST expects URL-encoded *path* segments for ``conversationId`` (and often ``activityId``).

The stock ``microsoft-teams-api`` client interpolates raw IDs into ``f\".../v3/conversations/{id}/...\"``,
so IDs such as ``19:...@thread.tacv2`` are sent with reserved characters unencoded and the service
returns 404. We patch the SDK to ``urllib.parse.quote(..., safe=\"\")`` for path segments only; JSON
activity bodies are unchanged.
"""

from __future__ import annotations

from urllib.parse import quote

_patched: bool = False

_PATH_SAFE = ""


def _p(seg: str) -> str:
    return quote(str(seg), safe=_PATH_SAFE)


def apply() -> None:
    """Idempotent. Call before any Teams Bot Framework REST use."""
    global _patched
    if _patched:
        return
    from microsoft_teams.api.clients.conversation import activity as activity_mod
    from microsoft_teams.api.clients.conversation import member as member_mod

    ca = activity_mod.ConversationActivityClient
    cm = member_mod.ConversationMemberClient

    def _m(cls, name: str, impl):
        setattr(cls, name, impl)

    # --- activities ---
    # pylint: disable=protected-access
    _orig_create = ca.create
    _orig_update = ca.update
    _orig_reply = ca.reply
    _orig_delete = ca.delete
    _orig_get_members = ca.get_members
    _orig_create_targeted = ca.create_targeted
    _orig_update_targeted = ca.update_targeted
    _orig_delete_targeted = ca.delete_targeted

    async def create(self, conversation_id, activity):
        return await _orig_create(self, _p(conversation_id), activity)

    async def update(self, conversation_id, activity_id, activity):
        return await _orig_update(self, _p(conversation_id), _p(activity_id), activity)

    async def reply(self, conversation_id, activity_id, activity):
        return await _orig_reply(self, _p(conversation_id), _p(activity_id), activity)

    async def delete(self, conversation_id, activity_id):
        return await _orig_delete(self, _p(conversation_id), _p(activity_id))

    async def get_members(self, conversation_id, activity_id):
        return await _orig_get_members(self, _p(conversation_id), _p(activity_id))

    async def create_targeted(self, conversation_id, activity):
        return await _orig_create_targeted(self, _p(conversation_id), activity)

    async def update_targeted(self, conversation_id, activity_id, activity):
        return await _orig_update_targeted(self, _p(conversation_id), _p(activity_id), activity)

    async def delete_targeted(self, conversation_id, activity_id):
        return await _orig_delete_targeted(self, _p(conversation_id), _p(activity_id))

    _m(ca, "create", create)
    _m(ca, "update", update)
    _m(ca, "reply", reply)
    _m(ca, "delete", delete)
    _m(ca, "get_members", get_members)
    _m(ca, "create_targeted", create_targeted)
    _m(ca, "update_targeted", update_targeted)
    _m(ca, "delete_targeted", delete_targeted)

    # --- members ---
    _orig_m_get = cm.get
    _orig_m_paged = cm.get_paged
    _orig_m_get_by_id = cm.get_by_id

    async def m_get(self, conversation_id):
        return await _orig_m_get(self, _p(conversation_id))

    async def m_get_paged(self, conversation_id, page_size=None, continuation_token=None):
        return await _orig_m_paged(self, _p(conversation_id), page_size, continuation_token)

    async def m_get_by_id(self, conversation_id, member_id):
        return await _orig_m_get_by_id(self, _p(conversation_id), _p(member_id))

    _m(cm, "get", m_get)
    _m(cm, "get_paged", m_get_paged)
    _m(cm, "get_by_id", m_get_by_id)

    _patched = True
