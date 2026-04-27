"""Shared boto3 clients and config for the access-requester Lambda (Slack and Teams)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3

import config

_ctx: RequesterContext | None = None


@dataclass
class RequesterContext:
    """Boto3 clients and config, built once per cold start."""

    cfg: config.Config
    schedule_client: Any
    org_client: Any
    sso_client: Any
    identity_store_client: Any
    s3_client: Any


def get_requester_context() -> RequesterContext:
    global _ctx
    if _ctx is None:
        c = config.get_config()
        session = boto3.Session()
        _ctx = RequesterContext(
            cfg=c,
            schedule_client=session.client("scheduler"),
            org_client=session.client("organizations"),
            sso_client=session.client("sso-admin"),
            identity_store_client=session.client("identitystore"),
            s3_client=session.client("s3"),
        )
    return _ctx
