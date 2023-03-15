
def config_lookup(section, lookup_filed_name=None, lookup_filed_value=None, return_field_name=None):
    if section not in CONFIG:
        raise KeyError(f'Can not find section={section} in config sections {CONFIG.keys()}')

    if lookup_filed_name is None and lookup_filed_value is None and return_field_name is None:
        return CONFIG[section]

    for item in CONFIG[section]:
        if item[lookup_filed_name] == lookup_filed_value:
            return item[return_field_name]
    else:
        raise KeyError(f'Can not find key={lookup_filed_name} value={lookup_filed_value} in section {section}')

# FIXME should come as config from outside
CONFIG = {
    'sso_instance:': 'arn',
    'users': [
        {
            'sso_id': '9067639464-9039c8fc-c296-4863-a233-a59111da7aa3',
            'email': 'email',
            'slack_id': 'U6AMA1LA3',
            'can_approve': True
        },
        {
            'sso_id': 'e448c4a8-60c1-703e-2cf0-c46832b94173',
            'email': 'email',
            'slack_id': 'U047L1JT1L5',
            'can_approve': False
        },
    ],
    'permission_sets': [
        {'name': 'AdministratorAccess',
         'arn': 'arn'},
         {'name': 'SystemAdministrator',
         'arn': 'arn'},
        {'name': 'ReadOnly',
         'arn': 'arn'},        
    ],
    'accounts': [
        {
            'name': 'Account name',
            'id': 'account id',
            'approvers': ['email1', 'email2']

        }
    ]
}