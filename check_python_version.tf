# When the local Python version is not 3.10.10, pip will not install some packages.
# Therefore, we need to check the Python version before deployment.
resource "null_resource" "version_check" {
  provisioner "local-exec" {
    command = <<-EOF
      version=$(python --version 2>&1 | awk '{print $2}')
      required_version="3.10.10"
      if [ "$version" != "$required_version" ]; then
        echo "Local python version is incorrect: $version. Required version is $required_version. Please clean "builds", and then use docker for deployment, or destroy and re-create sso_elevator with the correct python version."
        exit 1
      fi
    EOF
  }
  triggers = {
    always_run = "${timestamp()}"
  }
}
