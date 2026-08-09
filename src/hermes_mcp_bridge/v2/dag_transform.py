"""Phase 5 TRANSFORM engine — closed, pure, typed operation set (OD-024 closed).

> **V2 · PHASE 5 · runtime, disabled by default behind ``DAG_FEATURE_ENABLED``**

Decision (ADR-0026): the transform layer is a **fixed table of named
operations**, not a DSL. There is no expression parser, no template engine, no
JSONPath, no lambda, no user-supplied code, and no dynamic dispatch by string
into arbitrary callables. ``eval``/``exec``/``compile``/``__import__`` never
appear, and the module imports no I/O, clock, randomness or network facility.

Every operation:

* declares an input type per argument and an output type;
* is deterministic — same inputs, same output bytes;
* is total — a type or size violation raises
  :class:`~hermes_mcp_bridge.v2.dag_contract.PlanValidationError`, never a
  partial result;
* is size-bounded — the canonical byte length of the output is checked against
  the node budget before the value is returned.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from .canonical import canonical_json_bytes
from .dag_contract import (
    DAG_MAX_RESULT_BYTES,
    PlanReason,
    PlanValidationError,
)

#: Declared type vocabulary. Deliberately tiny and closed.
TYPE_OBJECT: Final = "object"
TYPE_LIST: Final = "list"
TYPE_STRING: Final = "string"
TYPE_INT: Final = "int"
TYPE_BOOL: Final = "bool"
TYPE_ANY: Final = "any"

TYPE_NAMES: Final[frozenset[str]] = frozenset(
    {TYPE_OBJECT, TYPE_LIST, TYPE_STRING, TYPE_INT, TYPE_BOOL, TYPE_ANY}
)


def type_of(value: Any) -> str:
    if isinstance(value, bool):
        return TYPE_BOOL
    if isinstance(value, int):
        return TYPE_INT
    if isinstance(value, str):
        return TYPE_STRING
    if isinstance(value, Mapping):
        return TYPE_OBJECT
    if isinstance(value, Sequence):
        return TYPE_LIST
    raise PlanValidationError(PlanReason.TRANSFORM_TYPE_MISMATCH, "unsupported value type")


def types_compatible(declared: str, actual: str) -> bool:
    """Equality, with ``any`` accepted only when explicitly declared on both ends."""
    if declared not in TYPE_NAMES or actual not in TYPE_NAMES:
        return False
    if declared == TYPE_ANY or actual == TYPE_ANY:
        return declared == actual
    return declared == actual


def _require(condition: bool, reason: PlanReason, detail: str) -> None:
    if not condition:
        raise PlanValidationError(reason, detail)


def _as_object(value: Any, name: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), PlanReason.TRANSFORM_TYPE_MISMATCH, f"{name}: object")
    return value  # type: ignore[return-value]


def _as_list(value: Any, name: str) -> Sequence[Any]:
    _require(
        isinstance(value, Sequence) and not isinstance(value, str | bytes),
        PlanReason.TRANSFORM_TYPE_MISMATCH,
        f"{name}: list",
    )
    return value  # type: ignore[return-value]


def _as_str(value: Any, name: str) -> str:
    _require(isinstance(value, str), PlanReason.TRANSFORM_TYPE_MISMATCH, f"{name}: string")
    return str(value)


def _field(item: Any, key: str) -> Any:
    """Single-level field read. No dotted traversal, no attribute access."""
    mapping = _as_object(item, "item")
    _require(key in mapping, PlanReason.TRANSFORM_TYPE_MISMATCH, "field: unknown")
    return mapping[key]


def _op_select(args: Mapping[str, Any]) -> Any:
    source = _as_object(args.get("value"), "value")
    key = _as_str(args.get("field"), "field")
    _require(key in source, PlanReason.TRANSFORM_TYPE_MISMATCH, "select: unknown field")
    return source[key]


def _op_project(args: Mapping[str, Any]) -> Any:
    source = _as_object(args.get("value"), "value")
    fields = _as_list(args.get("fields"), "fields")
    out: dict[str, Any] = {}
    for name in fields:
        key = _as_str(name, "fields[]")
        _require(key in source, PlanReason.TRANSFORM_TYPE_MISMATCH, "project: unknown field")
        out[key] = source[key]
    return out


def _op_filter_eq(args: Mapping[str, Any]) -> Any:
    items = _as_list(args.get("value"), "value")
    key = _as_str(args.get("field"), "field")
    expected = args.get("equals")
    return [item for item in items if _field(item, key) == expected]


def _op_filter_in(args: Mapping[str, Any]) -> Any:
    items = _as_list(args.get("value"), "value")
    key = _as_str(args.get("field"), "field")
    allowed = list(_as_list(args.get("values"), "values"))
    return [item for item in items if _field(item, key) in allowed]


def _op_map_field(args: Mapping[str, Any]) -> Any:
    items = _as_list(args.get("value"), "value")
    key = _as_str(args.get("field"), "field")
    return [_field(item, key) for item in items]


def _op_count(args: Mapping[str, Any]) -> Any:
    return len(_as_list(args.get("value"), "value"))


def _op_first(args: Mapping[str, Any]) -> Any:
    items = _as_list(args.get("value"), "value")
    _require(bool(items), PlanReason.TRANSFORM_TYPE_MISMATCH, "first: empty list")
    return items[0]


def _op_sort_by(args: Mapping[str, Any]) -> Any:
    items = list(_as_list(args.get("value"), "value"))
    key = _as_str(args.get("field"), "field")
    keyed = [(_field(item, key), index, item) for index, item in enumerate(items)]
    kinds = {type_of(entry[0]) for entry in keyed}
    _require(len(kinds) <= 1, PlanReason.TRANSFORM_TYPE_MISMATCH, "sort_by: mixed key types")
    # Stable, total ordering: (canonical key bytes, original index).
    keyed.sort(key=lambda entry: (canonical_json_bytes(entry[0]), entry[1]))
    return [entry[2] for entry in keyed]


def _op_unique(args: Mapping[str, Any]) -> Any:
    items = _as_list(args.get("value"), "value")
    seen: list[bytes] = []
    out: list[Any] = []
    for item in items:
        encoded = canonical_json_bytes(item)
        if encoded not in seen:
            seen.append(encoded)
            out.append(item)
    return out


def _op_merge_objects(args: Mapping[str, Any]) -> Any:
    left = _as_object(args.get("left"), "left")
    right = _as_object(args.get("right"), "right")
    overlap = sorted(set(left) & set(right))
    # Silent precedence would make the result order-dependent; fail closed.
    _require(not overlap, PlanReason.TRANSFORM_TYPE_MISMATCH, "merge_objects: key collision")
    return {**dict(left), **dict(right)}


def _op_to_list(args: Mapping[str, Any]) -> Any:
    value = args.get("value")
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return list(value)
    return [value]


def _op_require_non_empty(args: Mapping[str, Any]) -> Any:
    items = _as_list(args.get("value"), "value")
    _require(bool(items), PlanReason.TRANSFORM_TYPE_MISMATCH, "require_non_empty: empty")
    return list(items)


class TransformSpec:
    """Static declaration of one operation: arity, types and output type."""

    __slots__ = ("arg_types", "handler", "name", "output_type", "required_args")

    def __init__(
        self,
        name: str,
        required_args: tuple[str, ...],
        arg_types: Mapping[str, str],
        output_type: str,
        handler: Any,
    ) -> None:
        self.name = name
        self.required_args = required_args
        self.arg_types = dict(arg_types)
        self.output_type = output_type
        self.handler = handler


#: The closed operation table. Adding an entry is a reviewable code change and
#: bumps ``DAG_DIGEST_VERSION`` only if it changes existing semantics.
TRANSFORM_OPS: Final[Mapping[str, TransformSpec]] = {
    spec.name: spec
    for spec in (
        TransformSpec(
            "select",
            ("value", "field"),
            {"value": TYPE_OBJECT, "field": TYPE_STRING},
            TYPE_ANY,
            _op_select,
        ),
        TransformSpec(
            "project",
            ("value", "fields"),
            {"value": TYPE_OBJECT, "fields": TYPE_LIST},
            TYPE_OBJECT,
            _op_project,
        ),
        TransformSpec(
            "filter_eq",
            ("value", "field", "equals"),
            {"value": TYPE_LIST, "field": TYPE_STRING},
            TYPE_LIST,
            _op_filter_eq,
        ),
        TransformSpec(
            "filter_in",
            ("value", "field", "values"),
            {"value": TYPE_LIST, "field": TYPE_STRING, "values": TYPE_LIST},
            TYPE_LIST,
            _op_filter_in,
        ),
        TransformSpec(
            "map_field",
            ("value", "field"),
            {"value": TYPE_LIST, "field": TYPE_STRING},
            TYPE_LIST,
            _op_map_field,
        ),
        TransformSpec(
            "count",
            ("value",),
            {"value": TYPE_LIST},
            TYPE_INT,
            _op_count,
        ),
        TransformSpec(
            "first",
            ("value",),
            {"value": TYPE_LIST},
            TYPE_ANY,
            _op_first,
        ),
        TransformSpec(
            "sort_by",
            ("value", "field"),
            {"value": TYPE_LIST, "field": TYPE_STRING},
            TYPE_LIST,
            _op_sort_by,
        ),
        TransformSpec(
            "unique",
            ("value",),
            {"value": TYPE_LIST},
            TYPE_LIST,
            _op_unique,
        ),
        TransformSpec(
            "merge_objects",
            ("left", "right"),
            {"left": TYPE_OBJECT, "right": TYPE_OBJECT},
            TYPE_OBJECT,
            _op_merge_objects,
        ),
        TransformSpec(
            "to_list",
            ("value",),
            {},
            TYPE_LIST,
            _op_to_list,
        ),
        TransformSpec(
            "require_non_empty",
            ("value",),
            {"value": TYPE_LIST},
            TYPE_LIST,
            _op_require_non_empty,
        ),
    )
}


TRANSFORM_OP_NAMES: Final[tuple[str, ...]] = tuple(sorted(TRANSFORM_OPS))


def transform_output_type(op: str) -> str:
    spec = TRANSFORM_OPS.get(op)
    if spec is None:
        raise PlanValidationError(PlanReason.TRANSFORM_OP_UNKNOWN, op)
    return spec.output_type


def validate_transform_shape(op: str, arg_names: Sequence[str]) -> None:
    """Static check used by plan validation: op exists and arity is exact."""
    spec = TRANSFORM_OPS.get(op)
    if spec is None:
        raise PlanValidationError(PlanReason.TRANSFORM_OP_UNKNOWN, op)
    provided = set(arg_names)
    missing = sorted(set(spec.required_args) - provided)
    if missing:
        raise PlanValidationError(
            PlanReason.TRANSFORM_TYPE_MISMATCH, f"{op}: missing {','.join(missing)}"
        )
    extra = sorted(provided - set(spec.required_args))
    if extra:
        raise PlanValidationError(
            PlanReason.TRANSFORM_TYPE_MISMATCH, f"{op}: unexpected {','.join(extra)}"
        )


def apply_transform(
    op: str, args: Mapping[str, Any], *, max_bytes: int = DAG_MAX_RESULT_BYTES
) -> Any:
    """Apply one closed-set operation to already-resolved arguments."""
    spec = TRANSFORM_OPS.get(op)
    if spec is None:
        raise PlanValidationError(PlanReason.TRANSFORM_OP_UNKNOWN, op)
    validate_transform_shape(op, tuple(args))
    for name, declared in spec.arg_types.items():
        if declared is TYPE_ANY:
            continue
        actual = type_of(args[name])
        if actual != declared:
            raise PlanValidationError(
                PlanReason.TRANSFORM_TYPE_MISMATCH, f"{op}.{name}: {actual} != {declared}"
            )
    result = spec.handler(args)
    encoded = canonical_json_bytes(result)
    if len(encoded) > max_bytes:
        raise PlanValidationError(PlanReason.TRANSFORM_OUTPUT_TOO_LARGE, op)
    return result


__all__ = [
    "TRANSFORM_OPS",
    "TRANSFORM_OP_NAMES",
    "TYPE_ANY",
    "TYPE_BOOL",
    "TYPE_INT",
    "TYPE_LIST",
    "TYPE_NAMES",
    "TYPE_OBJECT",
    "TYPE_STRING",
    "TransformSpec",
    "apply_transform",
    "transform_output_type",
    "type_of",
    "types_compatible",
    "validate_transform_shape",
]
