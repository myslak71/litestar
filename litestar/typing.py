from __future__ import annotations

from collections import abc
from dataclasses import dataclass, replace
from inspect import Parameter, Signature
from typing import Any, AnyStr, Collection, ForwardRef, Literal, Mapping, Sequence, TypeVar, cast

from pydantic.fields import FieldInfo
from typing_extensions import Annotated, NotRequired, Required, get_args, get_origin

from litestar.exceptions import ImproperlyConfiguredException
from litestar.openapi.spec import Example
from litestar.params import BodyKwarg, DependencyKwarg, KwargDefinition, ParameterKwarg
from litestar.types import Empty
from litestar.types.builtin_types import NoneType, UnionTypes
from litestar.utils.predicates import (
    is_annotated_type,
    is_any,
    is_class_and_subclass,
    is_generic,
    is_non_string_iterable,
    is_non_string_sequence,
    is_pydantic_constrained_field,
)
from litestar.utils.typing import (
    get_instantiable_origin,
    get_safe_generic_origin,
    make_non_optional_union,
    unwrap_annotation,
)

__all__ = ("FieldDefinition",)

T = TypeVar("T", bound=KwargDefinition)


def _unpack_predicate(value: Any) -> dict[str, Any]:
    try:
        from annotated_types import Predicate

        if isinstance(value, Predicate):
            if value.func == str.islower:
                return {"lower_case": True}
            if value.func == str.isupper:
                return {"upper_case": True}
            if value.func == str.isascii:
                return {"pattern": "[[:ascii:]]"}
            if value.func == str.isdigit:
                return {"pattern": "[[:digit:]]"}
    except ImportError:
        pass

    return {}


def _parse_metadata(value: Any, is_sequence_container: bool, extra: dict[str, Any] | None) -> dict[str, Any]:
    """Parse metadata from a value.

    Args:
        value: A metadata value from annotation, namely anything stored under Annotated[x, metadata...]
        is_sequence_container: Whether the type is a sequence container (list, tuple etc...)
        extra: Extra key values to parse.

    Returns:
        A dictionary of constraints, which fulfill the kwargs of a KwargDefinition class.
    """
    extra = cast("dict[str, Any]", extra or getattr(value, "extra", None) or {})
    if example := extra.pop("example", None):
        example_list = [Example(value=example)]
    elif examples := getattr(value, "examples", None):
        example_list = [Example(value=example) for example in cast("list[str]", examples)]
    else:
        example_list = None

    return {
        k: v
        for k, v in {
            "gt": getattr(value, "gt", None),
            "ge": getattr(value, "ge", None),
            "lt": getattr(value, "lt", None),
            "le": getattr(value, "le", None),
            "multiple_of": getattr(value, "multiple_of", None),
            "min_length": None if is_sequence_container else getattr(value, "min_length", None),
            "max_length": None if is_sequence_container else getattr(value, "max_length", None),
            "description": getattr(value, "description", None),
            "examples": example_list,
            "title": getattr(value, "title", None),
            "lower_case": getattr(value, "to_lower", None),
            "upper_case": getattr(value, "to_upper", None),
            "pattern": getattr(value, "regex", getattr(value, "pattern", None)),
            "min_items": getattr(value, "min_items", getattr(value, "min_length", None))
            if is_sequence_container
            else None,
            "max_items": getattr(value, "max_items", getattr(value, "max_length", None))
            if is_sequence_container
            else None,
            "const": getattr(value, "const", None) is not None,
            **extra,
        }.items()
        if v is not None
    }


def _traverse_metadata(
    metadata: Sequence[Any], is_sequence_container: bool, extra: dict[str, Any] | None
) -> dict[str, Any]:
    """Recursively traverse metadata from a value.

    Args:
        metadata: A list of metadata values from annotation, namely anything stored under Annotated[x, metadata...]
        is_sequence_container: Whether or not the container is a sequence container (list, tuple etc...)
        extra: Extra key values to parse.

    Returns:
        A dictionary of constraints, which fulfill the kwargs of a KwargDefinition class.
    """
    constraints: dict[str, Any] = {}
    for value in metadata:
        if is_annotated_type(value) and (type_args := [v for v in get_args(value) if v is not None]):
            # annotated values can be nested inside other annotated values
            # this behaviour is buggy in python 3.8, hence we need to guard here.
            if len(type_args) > 1:
                constraints.update(
                    _traverse_metadata(metadata=type_args[1:], is_sequence_container=is_sequence_container, extra=extra)
                )
        elif unpacked_predicate := _unpack_predicate(value):
            constraints.update(unpacked_predicate)
        else:
            constraints.update(_parse_metadata(value=value, is_sequence_container=is_sequence_container, extra=extra))
    return constraints


def _create_metadata_from_type(
    metadata: Sequence[Any], model: type[T], annotation: Any, extra: dict[str, Any] | None
) -> tuple[T | None, dict[str, Any]]:
    is_sequence_container = is_non_string_sequence(annotation)
    result = _traverse_metadata(metadata=metadata, is_sequence_container=is_sequence_container, extra=extra)

    constraints = {k: v for k, v in result.items() if k in dir(model)}
    extra = {k: v for k, v in result.items() if k not in constraints}
    return model(**constraints) if constraints else None, extra


@dataclass(frozen=True)
class FieldDefinition:
    """Represents a function parameter or type annotation."""

    __slots__ = (
        "annotation",
        "args",
        "default",
        "extra",
        "inner_types",
        "instantiable_origin",
        "kwarg_definition",
        "metadata",
        "name",
        "origin",
        "raw",
        "safe_generic_origin",
        "type_wrappers",
    )

    raw: Any
    """The annotation exactly as received."""
    annotation: Any
    """The annotation with any "wrapper" types removed, e.g. Annotated."""
    type_wrappers: tuple[type, ...]
    """A set of all "wrapper" types, e.g. Annotated."""
    origin: Any
    """The result of calling ``get_origin(annotation)`` after unwrapping Annotated, e.g. list."""
    args: tuple[Any, ...]
    """The result of calling ``get_args(annotation)`` after unwrapping Annotated, e.g. (int,)."""
    metadata: tuple[Any, ...]
    """Any metadata associated with the annotation via ``Annotated``."""
    instantiable_origin: Any
    """An equivalent type to ``origin`` that can be safely instantiated. E.g., ``Sequence`` -> ``list``."""
    safe_generic_origin: Any
    """An equivalent type to ``origin`` that can be safely used as a generic type across all supported Python versions.

    This is to serve safely rebuilding a generic outer type with different args at runtime.
    """
    inner_types: tuple[FieldDefinition, ...]
    """The type's generic args parsed as ``FieldDefinition``, if applicable."""
    default: Any
    """Default value of the field."""
    extra: dict[str, Any]
    """A mapping of extra values."""
    kwarg_definition: KwargDefinition | DependencyKwarg | None
    """Kwarg Parameter."""
    name: str
    """Field name."""

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, FieldDefinition):
            return False

        if self.origin:
            return self.origin == other.origin and self.inner_types == other.inner_types

        return self.annotation == other.annotation  # type: ignore[no-any-return]

    def __hash__(self) -> int:
        return hash((self.name, self.raw, self.annotation, self.origin, self.inner_types))

    @classmethod
    def _extract_metadata(
        cls, annotation: Any, name: str | None, default: Any, metadata: tuple[Any, ...], extra: dict[str, Any] | None
    ) -> tuple[KwargDefinition | None, dict[str, Any]]:
        from litestar.dto.factory.base import AbstractDTOFactory

        model = BodyKwarg if name == "data" else ParameterKwarg
        if isinstance(default, FieldInfo):
            return _create_metadata_from_type(metadata=[default], model=model, annotation=annotation, extra=extra)

        if is_pydantic_constrained_field(annotation) or isinstance(annotation, AbstractDTOFactory):
            return _create_metadata_from_type(metadata=[annotation], model=model, annotation=annotation, extra=extra)

        if any(isinstance(arg, KwargDefinition) for arg in get_args(annotation)):
            return next(arg for arg in get_args(annotation) if isinstance(arg, KwargDefinition)), extra or {}

        if metadata:
            return _create_metadata_from_type(metadata=metadata, model=model, annotation=annotation, extra=extra)

        return None, {}

    @property
    def has_default(self) -> bool:
        """Check if the field has a default value.

        Returns:
            True if the default is not Empty or Ellipsis otherwise False.
        """
        return self.default is not Empty and self.default is not Ellipsis

    @property
    def is_non_string_iterable(self) -> bool:
        """Check if the field type is an Iterable.

        If ``self.annotation`` is an optional union, only the non-optional members of the union are evaluated.

        See: https://github.com/litestar-org/litestar/issues/1106
        """
        annotation = self.annotation
        if self.is_optional:
            annotation = make_non_optional_union(annotation)
        return is_non_string_iterable(annotation)

    @property
    def is_non_string_sequence(self) -> bool:
        """Check if the field type is a non-string Sequence.

        If ``self.annotation`` is an optional union, only the non-optional members of the union are evaluated.

        See: https://github.com/litestar-org/litestar/issues/1106
        """
        annotation = self.annotation
        if self.is_optional:
            annotation = make_non_optional_union(annotation)
        return is_non_string_sequence(annotation)

    @property
    def is_any(self) -> bool:
        """Check if the field type is Any."""
        return is_any(self.annotation)

    @property
    def is_generic(self) -> bool:
        """Check if the field type is a custom class extending Generic."""
        return is_generic(self.annotation)

    @property
    def is_simple_type(self) -> bool:
        """Check if the field type is a singleton value (e.g. int, str etc.)."""
        return not (
            self.is_generic or self.is_optional or self.is_union or self.is_mapping or self.is_non_string_iterable
        )

    @property
    def is_parameter_field(self) -> bool:
        """Check if the field type is a parameter kwarg value."""
        return isinstance(self.kwarg_definition, ParameterKwarg)

    @property
    def is_const(self) -> bool:
        """Check if the field is defined as constant value."""
        return bool(self.kwarg_definition and getattr(self.kwarg_definition, "const", False))

    @property
    def is_required(self) -> bool:
        """Check if the field should be marked as a required parameter."""
        if Required in self.type_wrappers:  # type: ignore[comparison-overlap]
            return True
        if NotRequired in self.type_wrappers:  # type: ignore[comparison-overlap]
            return False

        if isinstance(self.kwarg_definition, ParameterKwarg) and self.kwarg_definition.required is not None:
            return self.kwarg_definition.required

        return not self.is_optional and not self.is_any and (not self.has_default or self.default is None)

    @property
    def is_annotated(self) -> bool:
        """Check if the field type is Annotated."""
        return Annotated in self.type_wrappers  # type: ignore[comparison-overlap]

    @property
    def is_literal(self) -> bool:
        """Check if the field type is Literal."""
        return get_origin(self.annotation) is Literal

    @property
    def is_forward_ref(self) -> bool:
        """Whether the annotation is a forward reference or not."""
        return isinstance(self.annotation, (str, ForwardRef))

    @property
    def is_mapping(self) -> bool:
        """Whether the annotation is a mapping or not."""
        return self.is_subclass_of(Mapping)

    @property
    def is_tuple(self) -> bool:
        """Whether the annotation is a ``tuple`` or not."""
        return self.is_subclass_of(tuple)

    @property
    def is_type_var(self) -> bool:
        """Whether the annotation is a TypeVar or not."""
        return isinstance(self.annotation, TypeVar)

    @property
    def is_union(self) -> bool:
        """Whether the annotation is a union type or not."""
        return self.origin in UnionTypes

    @property
    def is_optional(self) -> bool:
        """Whether the annotation is Optional or not."""
        return bool(self.is_union and NoneType in self.args)

    @property
    def is_collection(self) -> bool:
        """Whether the annotation is a collection type or not."""
        return self.is_subclass_of(Collection)

    @property
    def is_non_string_collection(self) -> bool:
        """Whether the annotation is a non-string collection type or not."""
        return self.is_collection and not self.is_subclass_of((str, bytes))

    def is_subclass_of(self, cl: type[Any] | tuple[type[Any], ...]) -> bool:
        """Whether the annotation is a subclass of the given type.

        Where ``self.annotation`` is a union type, this method will return ``True`` when all members of the union are
        a subtype of ``cl``, otherwise, ``False``.

        Args:
            cl: The type to check, or tuple of types. Passed as 2nd argument to ``issubclass()``.

        Returns:
            Whether the annotation is a subtype of the given type(s).
        """
        if self.origin:
            if self.origin in UnionTypes:
                return all(t.is_subclass_of(cl) for t in self.inner_types)

            return self.origin not in UnionTypes and is_class_and_subclass(self.origin, cl)

        if self.annotation is AnyStr:
            return is_class_and_subclass(str, cl) or is_class_and_subclass(bytes, cl)

        return self.annotation is not Any and not self.is_type_var and is_class_and_subclass(self.annotation, cl)

    def has_inner_subclass_of(self, cl: type[Any] | tuple[type[Any], ...]) -> bool:
        """Whether any generic args are a subclass of the given type.

        Args:
            cl: The type to check, or tuple of types. Passed as 2nd argument to ``issubclass()``.

        Returns:
            Whether any of the type's generic args are a subclass of the given type.
        """
        return any(t.is_subclass_of(cl) for t in self.inner_types)

    @classmethod
    def from_annotation(cls, annotation: Any, **kwargs: Any) -> FieldDefinition:
        """Initialize FieldDefinition.

        Args:
            annotation: The type annotation. This should be extracted from the return of
                ``get_type_hints(..., include_extras=True)`` so that forward references are resolved and recursive
                ``Annotated`` types are flattened.
            **kwargs: Additional keyword arguments to pass to the ``FieldDefinition`` constructor.

        Returns:
            FieldDefinition
        """

        unwrapped, metadata, wrappers = unwrap_annotation(annotation if annotation is not Empty else Any)
        origin = get_origin(unwrapped)

        args = () if origin is abc.Callable else get_args(unwrapped)

        if not kwargs.get("kwarg_definition"):
            if isinstance(kwargs.get("default"), (KwargDefinition, DependencyKwarg)):
                kwargs["kwarg_definition"] = kwargs.pop("default")
            elif any(isinstance(v, (KwargDefinition, DependencyKwarg)) for v in metadata):
                kwargs["kwarg_definition"] = [v for v in metadata if isinstance(v, (KwargDefinition, DependencyKwarg))][
                    0
                ]
                metadata = tuple(v for v in metadata if not isinstance(v, (KwargDefinition, DependencyKwarg)))
            elif (extra := kwargs.get("extra", {})) and "kwarg_definition" in extra:
                kwargs["kwarg_definition"] = extra.pop("kwarg_definition")
            else:
                kwargs["kwarg_definition"], kwargs["extra"] = cls._extract_metadata(
                    annotation=annotation,
                    name=kwargs.get("name", ""),
                    default=kwargs.get("default", Empty),
                    metadata=metadata,
                    extra=kwargs.get("extra", {}),
                )

        kwargs.setdefault("annotation", unwrapped)
        kwargs.setdefault("args", args)
        kwargs.setdefault("default", Empty)
        kwargs.setdefault("extra", {})
        kwargs.setdefault("inner_types", tuple(FieldDefinition.from_annotation(arg) for arg in args))
        kwargs.setdefault("instantiable_origin", get_instantiable_origin(origin, unwrapped))
        kwargs.setdefault("kwarg_definition", None)
        kwargs.setdefault("metadata", metadata)
        kwargs.setdefault("name", "")
        kwargs.setdefault("origin", origin)
        kwargs.setdefault("raw", annotation)
        kwargs.setdefault("safe_generic_origin", get_safe_generic_origin(origin, unwrapped))
        kwargs.setdefault("type_wrappers", wrappers)

        instance = FieldDefinition(**kwargs)
        if not instance.has_default and instance.kwarg_definition:
            return replace(instance, default=instance.kwarg_definition.default)

        return instance

    @classmethod
    def from_kwarg(
        cls,
        annotation: Any,
        name: str,
        default: Any = Empty,
        inner_types: tuple[FieldDefinition, ...] | None = None,
        kwarg_definition: KwargDefinition | DependencyKwarg | None = None,
        extra: dict[str, Any] | None = None,
    ) -> FieldDefinition:
        """Create a new FieldDefinition instance.

        Args:
            annotation: The type of the kwarg.
            name: Field name.
            default: A default value.
            inner_types: A tuple of FieldDefinition instances representing the inner types, if any.
            kwarg_definition: Kwarg Parameter.
            extra: A mapping of extra values.

        Returns:
            FieldDefinition instance.
        """

        return cls.from_annotation(
            annotation,
            name=name,
            **{
                k: v
                for k, v in {
                    "inner_types": inner_types,
                    "kwarg_definition": kwarg_definition,
                    "extra": extra,
                    "default": default,
                }.items()
                if v is not None
            },
        )

    @classmethod
    def from_parameter(cls, parameter: Parameter, fn_type_hints: dict[str, Any]) -> FieldDefinition:
        """Initialize ParsedSignatureParameter.

        Args:
            parameter: inspect.Parameter
            fn_type_hints: mapping of names to types. Should be result of ``get_type_hints()``, preferably via the
                :attr:``get_fn_type_hints() <.utils.signature_parsing.get_fn_type_hints>`` helper.

        Returns:
            ParsedSignatureParameter.

        """
        from litestar.datastructures import ImmutableState

        try:
            annotation = fn_type_hints[parameter.name]
        except KeyError as e:
            raise ImproperlyConfiguredException(
                f"'{parameter.name}' does not have a type annotation. If it should receive any value, use 'Any'."
            ) from e

        if parameter.name == "state" and not issubclass(annotation, ImmutableState):
            raise ImproperlyConfiguredException(
                f"The type annotation `{annotation}` is an invalid type for the 'state' reserved kwarg. "
                "It must be typed to a subclass of `litestar.datastructures.ImmutableState` or "
                "`litestar.datastructures.State`."
            )

        return FieldDefinition.from_kwarg(
            annotation=annotation,
            name=parameter.name,
            default=Empty if parameter.default is Signature.empty else parameter.default,
        )
