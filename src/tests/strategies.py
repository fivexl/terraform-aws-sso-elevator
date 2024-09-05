import json
import string
from typing import Literal

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

# ruff: noqa: ANN201


def jsonstr(strategy: SearchStrategy) -> SearchStrategy:
    return st.builds(
        json.dumps,
        strategy,
    )


# https://docs.aws.amazon.com/organizations/latest/APIReference/API_CreateAccountStatus.html
aws_account_id = st.text(min_size=12, max_size=12, alphabet=string.digits)

# group draw:
group_id = st.text(min_size=36, max_size=36, alphabet=string.ascii_letters + string.digits + "-")

# https://docs.aws.amazon.com/singlesignon/latest/APIReference/API_CreatePermissionSet.html#singlesignon-CreatePermissionSet-request-Name
aws_permission_set_name = st.text(min_size=1, max_size=32, alphabet=string.ascii_letters + string.digits + "_+=,.@-")

# https://docs.aws.amazon.com/organizations/latest/APIReference/API_CreateOrganizationalUnit.html#organizations-CreateOrganizationalUnit-request-Name
aws_organization_unit_name = st.text(min_size=1, max_size=128, alphabet=string.ascii_letters)

statement_approvers = st.frozensets(st.emails(), min_size=1, max_size=10)

str_bool = st.one_of(st.just(str(True)), st.just(str(False)))

json_safe_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc", "Cf", "Co", "Cn"),
        blacklist_characters=("/"),
    ),
    min_size=1,
    max_size=200,
)


def resource_type_st(resource_type: Literal["Account", "OU", "Any"] = "Any"):
    if resource_type == "Account":
        return st.just("Account")
    elif resource_type == "OU":
        return st.just("OU")
    elif resource_type == "Any":
        return st.sampled_from(["Account", "OU"])
    raise ValueError(f"Unknown resource type: {resource_type}")


@st.composite
def resource(draw: st.DrawFn, resource_type: SearchStrategy[str]):
    r_type = draw(resource_type)
    if r_type == "Account":
        return draw(aws_account_id)
    elif r_type == "OU":
        return draw(aws_organization_unit_name)
    raise ValueError(f"Unknown resource type: {r_type}")


def statement_dict(
    resource_type: Literal["Account", "OU", "Any"] = "Account",
):
    resource_type_strategy = st.shared(resource_type_st(resource_type))
    resource_strategy = resource(resource_type_strategy)
    return st.fixed_dictionaries(
        mapping={
            "ResourceType": resource_type_strategy,
            "Resource": st.one_of(resource_strategy, st.lists(resource_strategy, max_size=20), st.just("*")),
            "PermissionSet": st.one_of(
                aws_permission_set_name,
                st.lists(aws_permission_set_name, max_size=20),
                st.just("*"),
            ),
        },
        optional={
            "Approvers": st.one_of(st.emails(), st.lists(st.emails(), max_size=20)), #type: ignore no
            "ApprovalIsNotRequired": st.booleans(),
            "AllowSelfApproval": st.booleans(),
        },
    )


@st.composite
def group_resource(draw: st.DrawFn, ):
    return draw(group_id)

def group_statement_dict():
    resource_strategy = group_resource()
    return st.fixed_dictionaries(
        mapping={
            "Resource": st.one_of(resource_strategy, st.lists(resource_strategy, max_size=20), st.just("*")),
        },
        optional={
            "Approvers": st.one_of(st.emails(), st.lists(st.emails(), max_size=20)),
            "ApprovalIsNotRequired": st.booleans(),
            "AllowSelfApproval": st.booleans(),
        },# type: ignore # noqa: PGH003
    )
