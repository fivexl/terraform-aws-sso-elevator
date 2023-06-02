import json
import sys

# Read the JSON data passed from Terraform
raw_data = sys.stdin.read()
data = json.loads(raw_data)

required_version = data["required_version"]

# Get the current Python version
current_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

if current_version != required_version:
    # Write the error message to stderr
    sys.stderr.write(
        f"Local python version is incorrect: {current_version}. Required version is {required_version}. Please clean 'builds', and then use docker for deployment, or destroy and re-create sso_elevator with the correct python version."  # noqa: E501
    )
    # Exit with a status code of 1, indicating failure
    sys.exit(1)

print(json.dumps({"current_version": current_version}))
