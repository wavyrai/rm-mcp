"""
Response helpers for MCP tools.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional


class DateTimeEncoder(json.JSONEncoder):
    """JSON encoder that handles datetime objects."""

    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def make_response(data: Dict[str, Any], hint: str, compact: bool = False) -> str:
    """Create a JSON response with a hint for the model."""
    if not compact:
        data["_hint"] = hint
    return json.dumps(data, indent=2, cls=DateTimeEncoder)


def make_error(
    error_type: str,
    message: str,
    suggestion: str,
    did_you_mean: Optional[List[str]] = None,
    compact: bool = False,
) -> str:
    """Create an educational error response."""
    error_body: Dict[str, Any] = {"type": error_type, "message": message}
    if not compact:
        error_body["suggestion"] = suggestion
        if did_you_mean:
            error_body["did_you_mean"] = did_you_mean
    error: Dict[str, Any] = {"_error": error_body}
    return json.dumps(error, indent=2, cls=DateTimeEncoder)
