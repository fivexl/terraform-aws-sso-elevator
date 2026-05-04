"""Shared dependency bundle for Microsoft Teams (requester + revoker)."""

from __future__ import annotations

from dataclasses import dataclass

import config
from mypy_boto3_identitystore import IdentityStoreClient
from mypy_boto3_organizations import OrganizationsClient
from mypy_boto3_s3 import S3Client
from mypy_boto3_scheduler import EventBridgeSchedulerClient
from mypy_boto3_sso_admin import SSOAdminClient


@dataclass
class TeamsDependencies:
    """AWS + config for Teams handlers and proactive messaging."""

    cfg: config.Config
    org_client: OrganizationsClient
    s3_client: S3Client
    sso_client: SSOAdminClient
    identity_store_client: IdentityStoreClient
    schedule_client: EventBridgeSchedulerClient
