from typing import Optional
from pydantic import BaseSettings, Field


class SlackConfig(BaseSettings):
    bot_token: str = Field(..., env="SLACK_BOT_TOKEN", min_length=1)
    signing_secret: str = Field(..., env="SLACK_SIGNING_SECRET", min_length=1)
    channel_id: str = Field(..., env="SLACK_CHANNEL_ID", min_length=1)


class Config(BaseSettings):
    post_update_to_slack: bool = False

    dynamodb_table_name: str
    sso_instance_arn: Optional[str] = None

    log_level: str = "INFO"
    config: dict

    def lookup(self, section, lookup_filed_name=None, lookup_filed_value=None, return_field_name=None):
        cfg = self.config
        if section not in cfg:
            raise KeyError(f"Can not find section={section} in config sections {cfg.keys()}")

        if lookup_filed_name is None and lookup_filed_value is None and return_field_name is None:
            return cfg[section]

        for item in cfg[section]:
            if item[lookup_filed_name] == lookup_filed_value:
                return item[return_field_name]
        raise KeyError(f"Can not find key={lookup_filed_name} value={lookup_filed_value} in section {section}")
