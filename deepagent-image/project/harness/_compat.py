"""Compatibility layer for optional dependencies.

Gracefully degrades when langchain is absent (e.g., on a bare test host).
"""


def compat_import(module_name: str, class_name: str):
    """Import a class, return object if the module is absent.

    Used when a module defines an AgentMiddleware but must work on a bare
    host without langchain installed (e.g., for unit tests of pure logic).

    Args:
        module_name: Full module path, e.g. "langchain.agents.middleware.types"
        class_name: Class to import, e.g. "AgentMiddleware"

    Returns:
        The imported class, or object if ModuleNotFoundError.
    """
    try:
        mod = __import__(module_name, fromlist=[class_name])
        return getattr(mod, class_name)
    except (ImportError, ModuleNotFoundError, AttributeError):
        return object
