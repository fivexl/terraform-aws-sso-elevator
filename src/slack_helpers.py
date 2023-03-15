import http.client
import json

# https://api.slack.com/surfaces/modals/using
# https://app.slack.com/block-kit-builder/
def prepare_slack_initial_form(trigger_id, permission_sets, accounts):
    return {
    'trigger_id': trigger_id,
    'view': {
        'type': 'modal',
        'callback_id': 'modal-identifier',
        'submit': {'type': 'plain_text', 'text': 'Request'},
        'close': {'type': 'plain_text', 'text': 'Cancel'},
        'title': {'type': 'plain_text', 'text': 'Get AWS access'},
        'blocks':
            [
                {
                    'type': 'section',
                    'text': {
                        'type': 'plain_text',
                        'text': ':wave: Hey! Please fill form below to request AWS access.'
                    }
                },
                {
                    'type': 'divider'
                },
                {
                    'block_id': 'select_role',
                    'type': 'input',
                    'label':
                        {
                            'type': 'plain_text',
                            'text': 'Select role to assume'
                        },
                    'element':
                        {
                            'action_id': 'selected_role',
                            'type': 'radio_buttons',
                            'options': [{'text': {'type': 'plain_text', 'text': permission_set['name']}, 'value': permission_set['name']} for permission_set in permission_sets]
                        }
                },
                {
                    'block_id': 'select_account',
                    'type': 'input',
                    'label':
                        {
                            'type': 'plain_text',
                            'text': 'Select AWS account'
                        },
                    'element':
                        {
                            'action_id': 'selected_account',
                            'type': 'static_select',
                            'placeholder': {
                                'type': 'plain_text',
                                'text': 'Select AWS account'
                            },
                            'options': [{'text': {'type': 'plain_text', 'text': account['name']}, 'value': account['id']} for account in accounts]
                        }
                },
                {
                    'block_id': 'provide_reason',
                    'type': 'input',
                    'label':
                        {
                            'type': 'plain_text',
                            'text': 'What is it you are going to do'
                        },
                    'element':
                        {
                            'action_id': 'provided_reason',
                            'type': 'plain_text_input',
                            'multiline': True
                        }
                },
                {
                    'type': 'divider'
                },
                {
                    'type': 'section',
                    'text': {
                        'type': 'plain_text',
                        'text': 'Remember to use access responsibly. All actions (AWS API calls) are being recorded.'
                    }
                }
            ]
        }
    }

def prepare_slack_approval_request(channel, requester_slack_id, account_id, requires_approval, role_name, reason):
    header_text = 'AWS account access request.'
    if requires_approval:
        header_text += '\n⚠️ This account does not allow self-approval ⚠️'
        header_text += '\nWe already contacted eligible approvers, wait for them to click the button.'
    can_self_approve = 'No' if requires_approval else 'Yes'
    return {
        'channel': channel,
        'blocks': [
            {
                'type': 'section',
                'block_id': 'header',
                'text': {
                    'type': 'mrkdwn',
                    'text': header_text
                }
            },
            {
                'type': 'section',
                'block_id': 'content',
                'fields': [
                    {
                        'type': 'mrkdwn',
                        'text': f'Requester: <@{requester_slack_id}>'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'AccountId: {account_id}'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'Role name: {role_name}'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'Can self-approve: {can_self_approve}'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'Reason: {reason}'
                    }
                ]
            },
            {
                'type': 'actions',
                'block_id': 'buttons',
                'elements': [
                    {
                        'type': 'button',
                        'action_id': 'approve',
                        'text': {
                            'type': 'plain_text',
                            'text': 'Approve'
                        },
                        'style': 'primary',
                        'value': 'approve'
                    },
                    {
                        'type': 'button',
                        'action_id': 'deny',
                        'text': {
                            'type': 'plain_text',
                            'text': 'Deny'
                        },
                        'style': 'danger',
                        'value': 'deny'
                    }
                ]
            }
        ]
    }


def find_value_in_content_block(blocks, key):
    for block in blocks:
        if block['block_id'] != 'content':
            continue
        for field in block['fields']:
            if field['text'].startswith(key):
                value = field['text'].split(': ')[1]
                return value.strip()
        else:
            raise KeyError(f'Can not find filed with key={key} in block {block}')


def prepare_slack_approval_request_update(channel, ts, approver, action, blocks):
    message = {
        'channel': channel,
        'ts': ts,
	    'blocks': []
    }
    # loop through original message and take header and content blocks to drop buttons
    for block in blocks:
        if block['block_id'] == 'header' or block['block_id'] == 'content':
            message['blocks'].append(block)
    # add information about approver
    message['blocks'].append(
        {
			'type': 'section',
            'block_id': 'footer',
			'text': {
				'type': 'mrkdwn',
				'text': f'<@{approver}> pressed {action} button'
			}
		}
    )
    return message

# POST https://slack.com/api/views.open
# Content-type: application/json
# Authorization: Bearer YOUR_ACCESS_TOKEN_HERE

def post_slack_message(api_path, message, token):
    print(f'Sending message: {json.dumps(message)}')
    headers = {'Content-type': 'application/json', 'Authorization': f'Bearer {token}'}
    connection = http.client.HTTPSConnection('slack.com')
    connection.request('POST',
                       api_path,
                       json.dumps(message),
                       headers)
    response = connection.getresponse()
    response_status = response.status
    response_body = json.loads(response.read().decode())
    print('Response: {}, message: {}'.format(response_status, response_body))
    return response_status, response_body