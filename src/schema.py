"""Load and validate against the JSON schemas in schemas/ (Draft 2020-12).

The schemas are the data contracts. They matter most for anything the LLM
returns: a response is validated before any code trusts it, so the model cannot
inject an unexpected field or the wrong type into the pipeline.
"""

from __future__ import annotations

import functools
import json
import os

import jsonschema

_SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schemas")


class SchemaError(RuntimeError):
    """An instance failed schema validation."""


@functools.lru_cache(maxsize=None)
def load(name: str) -> dict:
    """Load schemas/<name>.schema.json (cached)."""
    path = os.path.join(_SCHEMA_DIR, f"{name}.schema.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate(instance, name: str):
    """Validate `instance` against schemas/<name>.schema.json.

    Returns the instance on success; raises SchemaError with a readable message
    on failure."""
    try:
        jsonschema.validate(instance, load(name))
    except jsonschema.ValidationError as e:
        raise SchemaError(f"{name}: {e.message}") from e
    return instance


def is_valid(instance, name: str) -> bool:
    try:
        jsonschema.validate(instance, load(name))
        return True
    except jsonschema.ValidationError:
        return False
