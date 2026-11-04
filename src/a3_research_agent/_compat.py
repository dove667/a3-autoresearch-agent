"""Keep legacy checkout imports bound to their canonical implementation module."""

import importlib
import sys
from types import ModuleType


def redirect(name: str, target: str, namespace: dict) -> ModuleType:
    """Expose public attributes for file loaders and one module for normal imports."""
    module = importlib.import_module(target)
    namespace.update({key: value for key, value in vars(module).items() if not key.startswith("__")})
    namespace["__doc__"] = module.__doc__
    sys.modules[name] = module
    return module
