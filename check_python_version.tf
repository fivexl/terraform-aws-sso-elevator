# When the local Python version is not 3.10.10, pip will not install some packages.
# Therefore, we need to check the Python version before deployment.
data "external" "check_python_version" {
  count = var.build_in_docker == true ? 0 : 1

  program = ["python3", "${path.module}/src/check_python_version.py"]

  query = {
    required_version = local.full_python_version
  }
}

resource "null_resource" "python_version_check" {
  depends_on = [data.external.check_python_version]
}
