# terraform-aws-sso-slack-bot
Slack bot to temporary assign AWS SSO Permission set to a user



## Slack App manifest

Make sure to paste Lambda URL to `request_url` field

```
display_information:
  name: AWS SSO access elevator
features:
  bot_user:
    display_name: AWS SSO access elevator
    always_online: false
  shortcuts:
    - name: access
      type: global
      callback_id: acesss23
      description: Elevate AWS SSO access
oauth_config:
  scopes:
    bot:
      - commands
      - chat:write
settings:
  interactivity:
    is_enabled: true
    request_url: <LAMBDA URL GOES HERE>
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```