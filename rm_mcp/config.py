"""
Environment configuration helpers.

Every setting is read through these helpers so that a malformed value degrades
to the documented default with a warning, instead of raising at import time and
taking the whole server down before it can report why.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    """Read an integer setting from the environment.

    Args:
        name: Environment variable name.
        default: Value to use when unset, unparseable, or below `minimum`.
        minimum: Optional lower bound; values below it fall back to `default`.

    Returns:
        The parsed value, or `default` if the value is missing or invalid.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default

    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("%s=%r is not an integer — using default of %d", name, raw, default)
        return default

    if minimum is not None and value < minimum:
        logger.warning(
            "%s=%d is below the minimum of %d — using default of %d", name, value, minimum, default
        )
        return default

    return value


def env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean setting from the environment ('1', 'true', 'yes' are true)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
