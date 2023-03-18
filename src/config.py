import json

import base


def config_lookup(
    section, lookup_filed_name=None, lookup_filed_value=None, return_field_name=None
):
    if section not in CONFIG:
        raise KeyError(
            f"Can not find section={section} in config sections {CONFIG.keys()}"
        )

    if (
        lookup_filed_name is None
        and lookup_filed_value is None
        and return_field_name is None
    ):
        return CONFIG[section]

    for item in CONFIG[section]:
        if item[lookup_filed_name] == lookup_filed_value:
            return item[return_field_name]
    else:
        raise KeyError(
            f"Can not find key={lookup_filed_name} value={lookup_filed_value} in section {section}"
        )


CONFIG = json.loads(base.read_env_variable_or_die("CONFIG"))
