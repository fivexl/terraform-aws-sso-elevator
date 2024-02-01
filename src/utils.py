import random
import string


def generate_random_string(length: int) -> str:
    allowed_chars = string.ascii_letters + string.digits + "_+=,.@-"
    return "".join(random.choice(allowed_chars) for _ in range(length))


def gen_temporary_permission_set_name(original_name: str) -> str:
    length_of_random_string_for_name = 32 - len(original_name) - 4
    random_string = generate_random_string(length_of_random_string_for_name)
    return f"{original_name}-@E-{random_string}"
