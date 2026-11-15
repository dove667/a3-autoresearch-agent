"""Small, public validation contracts for client-executed research tools."""

from pathlib import PurePosixPath


class ToolInputError(ValueError):
    """Carry only deliberate, nonsecret validation advice to the model."""

    def __init__(self, code, hint):
        super().__init__(code)
        self.code, self.hint = code, hint


def fail(code, hint):
    raise ToolInputError(code, hint)


def definition(name, description, properties, required=()):
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        "strict": False,
    }


def arguments(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        fail("invalid_arguments", "Use the fields in this tool's schema; omit unrelated fields.")


def text(value, name, maximum=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        fail("invalid_" + name, f"Use nonempty {name} text, at most {maximum} characters.")
    return value


def integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        fail("invalid_" + name, f"Use an integer {name} in {minimum}..{maximum}.")
    return value


def boolean(value, name):
    if type(value) is not bool:
        fail("invalid_" + name, f"Use true or false for {name}.")
    return value


def relative_name(value, name="path"):
    text(value, name)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or path.as_posix() != value
        or "\\" in value
        or any(part in {".", ".."} or part.startswith(".") for part in path.parts)
        or any(ord(char) < 32 for char in value)
    ):
        fail("invalid_" + name, "Use a canonical relative path without hidden parts or traversal.")
    return value
