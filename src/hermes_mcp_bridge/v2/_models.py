"""Shared Pydantic base models for the V2 registry core.

Pydantic v2 wraps any ``ValueError`` raised inside a validator into a
``pydantic_core.ValidationError``. The V2 core exposes a *stable, typed* error
taxonomy (:mod:`hermes_mcp_bridge.v2.errors`), so these bases translate the
wrapped failure back into the canonical error class. Callers therefore always
see ``RegistryValidationError`` / ``PolicyValidationError`` regardless of
whether the failure came from a type check or a semantic invariant.

Error messages are derived from pydantic's own rendering, which contains only
field names and the validator message; validators in this package never place
credential material in those messages.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from .errors import PolicyValidationError, RegistryValidationError


class _TranslatingModel(BaseModel):
    """Base model translating ``ValidationError`` into a V2 error class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Overridden by subclasses.
    v2_error: ClassVar[type[Exception]] = RegistryValidationError

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise type(self).v2_error(str(exc)) from exc


class RegistryModel(_TranslatingModel):
    """Model whose validation failures surface as ``RegistryValidationError``."""

    v2_error: ClassVar[type[Exception]] = RegistryValidationError


class PolicyModel(_TranslatingModel):
    """Model whose validation failures surface as ``PolicyValidationError``."""

    v2_error: ClassVar[type[Exception]] = PolicyValidationError


__all__ = ["PolicyModel", "RegistryModel"]
