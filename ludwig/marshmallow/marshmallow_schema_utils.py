import json
import re
from dataclasses import field
from typing import Dict as tDict
from typing import List, Tuple, Union

from marshmallow import EXCLUDE, fields, schema, validate, ValidationError
from marshmallow_jsonschema import JSONSchema as js
from pytkdocs import loader as pytkloader

from ludwig.modules.reduction_modules import reduce_mode_registry
from ludwig.utils.torch_utils import initializer_registry

restloader = pytkloader.Loader(docstring_style="restructured-text")
googleloader = pytkloader.Loader(docstring_style="google")


def load_config(cls, **kwargs):
    """Takes a marshmallow class and instantiates it with the given keyword args.

    as parameters.
    """
    assert_is_a_marshmallow_class(cls)
    schema = cls.Schema()
    return schema.load(kwargs)


def load_config_with_kwargs(cls, kwargs):
    """Takes a marshmallow class and dict of parameter values and appropriately instantiantes the schema."""
    assert_is_a_marshmallow_class(cls)
    schema = cls.Schema()
    fields = schema.fields.keys()
    return load_config(cls, **{k: v for k, v in kwargs.items() if k in fields}), {
        k: v for k, v in kwargs.items() if k not in fields
    }


def create_cond(if_pred: tDict, then_pred: tDict):
    """Returns a JSONSchema conditional for the given if-then predicates."""
    return {
        "if": {"properties": {k: {"const": v} for k, v in if_pred.items()}},
        "then": {"properties": {k: v for k, v in then_pred.items()}},
    }


class BaseMarshmallowConfig:
    """Base marshmallow class for common attributes and metadata."""

    class Meta:
        """Sub-class specifying meta information for Marshmallow.

        Currently only sets `unknown` flag to `EXCLUDE`. This is done to mirror Ludwig behavior: unknown properties are
        excluded from `load` calls so that the marshmallow_dataclass package can be used but
        `get_custom_schema_from_marshmallow_class` will manually set a marshmallow schema's `additionalProperties` attr.
        to True so that JSON objects with extra properties do not raise errors; as a result properties are picked and
        filled in as necessary.
        """

        unknown = EXCLUDE
        "Flag that sets marshmallow `load` calls to ignore unknown properties passed as a parameter."


def assert_is_a_marshmallow_class(cls):
    assert hasattr(cls, "Schema") and isinstance(
        cls.Schema, schema.SchemaMeta
    ), f"Expected marshmallow class, but `{cls}` does not have the necessary `Schema` attribute."


def get_fully_qualified_class_name(cls):
    """Returns fully dot-qualified path of a class, e.g. `ludwig.models.trainer.TrainerConfig` given
    `TrainerConfig`."""
    return ".".join([cls.__module__, cls.__name__])


def unload_schema_from_marshmallow_jsonschema_dump(mclass) -> tDict:
    """Helper method to directly get a marshmallow class's JSON schema without extra wrapping props."""
    assert_is_a_marshmallow_class(mclass)
    return js().dump(mclass.Schema())["definitions"][mclass.__name__]


def get_custom_schema_from_marshmallow_class(mclass) -> tDict:
    """Get Ludwig-customized schema from a given marshmallow class."""
    assert_is_a_marshmallow_class(mclass)

    def cleanup_python_comment(dstring: str) -> str:
        """Cleans up some common issues with parsed comments/docstrings."""
        if dstring is None or dstring == "" or str.isspace(dstring):
            return ""
        # Add spaces after periods:
        dstring = re.sub(r"\.(?! )", ". ", dstring)
        # Replace internal newlines with spaces:
        dstring = re.sub("\n+", " ", dstring)
        # Replace any multiple-spaces with single spaces:
        dstring = re.sub(" +", " ", dstring)
        # Remove leading/ending spaces:
        dstring = dstring.strip()
        # Add final period if it's not there:
        dstring += "." if dstring[-1] != "." else ""
        # Capitalize first word in string and first word in each sentence.
        dstring = re.sub(r"((?<=[\.\?!]\s)(\w+)|(^\w+))", lambda m: m.group().capitalize(), dstring)
        return dstring

    def load_pytkdocs_json(name: str, is_torch=False):
        import os
        from pathlib import Path

        subfolder = "" if not is_torch else "torch/"
        relative_path = os.path.join("generated/", subfolder, name)
        parent_dir = str(Path(__file__).parent)
        full_path = os.path.join(parent_dir, relative_path) + ".json"
        with open(full_path) as input:
            return json.load(input)["objects"][0]

    def get_attrs_dict(cls):
        return {attr.split(".")[-1]: cls["children"][attr] for attr in cls["attributes"]}

    def get_torch_attrs_dict(cls):
        attrs_list = cls["docstring_sections"][1]["value"]
        return {attr["name"]: attr for attr in attrs_list}

    def generate_extra_json_schema_props(schema_cls) -> Dict:
        """Workaround for adding 'description' fields to a marshmallow schema's JSON Schema.

        Currently targeted for use with optimizer and combiner schema; if there is no description provided for a
        particular field, the description is pulled from the corresponding torch optimizer. Note that this currently
        overrides whatever may already be in the description/default fields. TODO(ksbrar): Watch this
        [issue](https://github.com/fuhrysteve/marshmallow-jsonschema/issues/41) to improve this eventually.
        """

        schema_dump = unload_schema_from_marshmallow_jsonschema_dump(schema_cls)
        if schema_cls.__doc__ is not None:
            # parsed_documentation = restloader.get_object_documentation(get_fully_qualified_class_name(schema_cls))
            parsed_documentation = load_pytkdocs_json(schema_cls.__name__)

            # Parse parents as well in case some attrs. are inherited:
            # parsed_parents = [
            #     restloader.get_object_documentation(get_fully_qualified_class_name(parent))
            #     for parent in schema_cls.__bases__
            # ]
            parsed_parents = [load_pytkdocs_json(parent.__name__) for parent in schema_cls.__bases__]

            # Add the top-level description to the schema if it exists:
            if parsed_documentation["docstring"] is not None:
                # schema_dump["description"] = cleanup_python_comment(parsed_documentation.docstring)
                schema_dump["description"] = cleanup_python_comment(parsed_documentation["docstring"])

            # Create a dictionary of all attributes (including possible inherited ones):
            # parsed_attrs = {attr.name: attr for attr in parsed_documentation.attributes}
            parsed_attrs = get_attrs_dict(parsed_documentation)
            parsed_parent_attrs = {}
            for parent in parsed_parents:
                # attrs = {attr.name: attr for attr in parent.attributes}
                attrs = get_attrs_dict(parent)

                parsed_parent_attrs = {**parsed_parent_attrs, **attrs}
            parsed_attrs = {**parsed_parent_attrs, **parsed_attrs}

            # For each prop in the schema, set its description and default if they are not already set. If not already
            # set and there is no available value from the Ludwig docstring, attempt to pull from PyTorch, if applicable
            # (e.g. for optimizer parameters).

            # parsed_torch = (
            #     {
            #         param.name: param
            #         for param in googleloader.get_object_documentation(
            #             get_fully_qualified_class_name(schema_cls.optimizer_class)
            #         )
            #         .docstring_sections[1]
            #         .value
            #     }
            #     if hasattr(schema_cls, "optimizer_class") and schema_cls.optimizer_class is not None
            #     else None
            # )
            parsed_torch = None
            if hasattr(schema_cls, "optimizer_class") and schema_cls.optimizer_class is not None:
                parsed_torch = get_torch_attrs_dict(
                    load_pytkdocs_json(schema_cls.optimizer_class.__name__, is_torch=True)
                )

            for prop in schema_dump["properties"]:
                schema_prop = schema_dump["properties"][prop]

                if prop in parsed_attrs:
                    # Handle descriptions:

                    # Get the particular attribute's docstring (if it has one), strip the default from the string:
                    parsed_docstring = parsed_attrs[prop]["docstring"]
                    if parsed_docstring is None:
                        parsed_docstring = ""

                    # Split the description and default (if they exist in the string):
                    parsed_desc = parsed_default = None
                    docstring_split = parsed_docstring.split("(default: ")
                    if len(docstring_split) == 2:
                        parsed_default = docstring_split[1]
                    parsed_desc = docstring_split[0]
                    if parsed_desc is None:
                        parsed_desc = ""

                    # If no description is provided, attempt to pull from torch if applicable (e.g. for optimizers):
                    desc = parsed_desc
                    print("-" * 50)
                    print(prop)
                    print(desc)
                    print(type(desc))
                    print(parsed_default)
                    print(type(parsed_default))
                    if (
                        desc == ""
                        and parsed_torch is not None
                        and prop in parsed_torch
                        and (parsed_torch[prop]["description"] is not None or parsed_torch[prop]["description"] != "")
                    ):
                        desc_split = parsed_torch[prop]["description"].split("(default: ")
                        if parsed_default is None and len(desc_split) == 2:
                            parsed_default = desc_split[1]
                        desc = cleanup_python_comment(desc_split[0])

                    print(desc)
                    print(parsed_default)
                    print("-" * 50)

                    # Add parsed default back to string if it exists:
                    if parsed_default is not None:
                        desc += f"(default: {parsed_default}"
                    schema_prop["description"] = cleanup_python_comment(desc)

        # Manual workaround because marshmallow_{dataclass,jsonschema} do not support setting this field (see above):
        schema_dump["additionalProperties"] = True
        return schema_dump

    return generate_extra_json_schema_props(mclass)


def InitializerOptions(default: Union[None, str] = None):
    return StringOptions(list(initializer_registry.keys()), default=default, nullable=True)


def ReductionOptions(default: Union[None, str] = None):
    return StringOptions(
        list(reduce_mode_registry.keys()),
        default=default,
        nullable=True,
    )


def RegularizerOptions(default: Union[None, str] = None, nullable: bool = True):
    return StringOptions(["l1", "l2", "l1_l2"], default=default, nullable=nullable)


def StringOptions(options: List[str], default: Union[None, str] = None, nullable: bool = True):
    # If None should be allowed for an enum field, it also has to be defined as a valid
    # [option](https://github.com/json-schema-org/json-schema-spec/issues/258):
    if len(options) <= 0:
        raise ValidationError("Must provide non-empty list of options!")
    if default is not None and not isinstance(default, str):
        raise ValidationError(f"Provided default `{default}` should be a string!")
    if nullable and None not in options:
        options += [None]
    if default not in options:
        raise ValidationError(f"Provided default `{default}` is not one of allowed options: {options} ")
    return field(
        metadata={
            "marshmallow_field": fields.String(
                validate=validate.OneOf(options),
                allow_none=nullable,
                default=default,
            )
        },
        default=default,
    )


def PositiveInteger(default: Union[None, int] = None):
    val = validate.Range(min=1)
    if default is not None:
        try:
            assert isinstance(default, int)
            val(default)
        except Exception:
            raise ValidationError(f"Invalid default: `{default}`")
    return field(
        metadata={
            "marshmallow_field": fields.Integer(strict=True, validate=val, allow_none=default is None, default=default)
        },
        default=default,
    )


def NonNegativeInteger(default: Union[None, int] = None):
    val = validate.Range(min=0)
    if default is not None:
        try:
            assert isinstance(default, int)
            val(default)
        except Exception:
            raise ValidationError(f"Invalid default: `{default}`")
    return field(
        metadata={
            "marshmallow_field": fields.Integer(strict=True, validate=val, allow_none=default is None, default=default)
        },
        default=default,
    )


def IntegerRange(default: Union[None, int] = None, **kwargs):
    val = validate.Range(**kwargs)
    if default is not None:
        try:
            assert isinstance(default, int)
            val(default)
        except Exception:
            raise ValidationError(f"Invalid default: `{default}`")
    return field(
        metadata={
            "marshmallow_field": fields.Integer(strict=True, validate=val, allow_none=default is None, default=default)
        },
        default=default,
    )


def NonNegativeFloat(default: Union[None, float] = None):
    val = validate.Range(min=0.0)
    if default is not None:
        try:
            assert isinstance(default, float) or isinstance(default, int)
            val(default)
        except Exception:
            raise ValidationError(f"Invalid default: `{default}`")
    return field(
        metadata={"marshmallow_field": fields.Float(validate=val, allow_none=default is None, default=default)},
        default=default,
    )


def FloatRange(default: Union[None, float] = None, **kwargs):
    val = validate.Range(**kwargs)
    if default is not None:
        try:
            assert isinstance(default, float) or isinstance(default, int)
            val(default)
        except Exception:
            raise ValidationError(f"Invalid default: `{default}`")
    return field(
        metadata={"marshmallow_field": fields.Float(validate=val, allow_none=default is None, default=default)},
        default=default,
    )


def Dict(default: Union[None, tDict] = None):
    if default is not None:
        try:
            assert isinstance(default, dict)
            assert all([isinstance(k, str) for k in default.keys()])
        except Exception:
            raise ValidationError(f"Invalid default: `{default}`")
    return field(
        metadata={"marshmallow_field": fields.Dict(fields.String(), allow_none=True, default=default)},
        default_factory=lambda: default,
    )


def DictList(default: Union[None, List[tDict]] = None):
    if default is not None:
        try:
            assert isinstance(default, list)
            assert all([isinstance(d, dict) for d in default])
            for d in default:
                assert all([isinstance(k, str) for k in d.keys()])
        except Exception:
            raise ValidationError(f"Invalid default: `{default}`")

    return field(
        metadata={"marshmallow_field": fields.List(fields.Dict(fields.String()), allow_none=True, default=default)},
        default_factory=lambda: default,
    )


def Embed():
    _embed_options = ["add"]

    # TODO(ksbrar): Should the default choice here be null?
    class EmbedInputFeatureNameField(fields.Field):
        def _deserialize(self, value, attr, data, **kwargs):
            if value is None:
                return value

            if isinstance(value, str):
                if value not in _embed_options:
                    raise ValidationError(f"Expected one of: {_embed_options}, found: {value}")
                return value

            if isinstance(value, int):
                return value

            raise ValidationError("Field should be int or str")

        def _jsonschema_type_mapping(self):
            return {"oneOf": [{"type": "string", "enum": _embed_options}, {"type": "integer"}, {"type": "null"}]}

    return field(
        metadata={"marshmallow_field": EmbedInputFeatureNameField(allow_none=True, default=None)}, default=None
    )


def InitializerOrDict(default: str = "xavier_uniform"):
    initializers = list(initializer_registry.keys())
    if not isinstance(default, str) or default not in initializers:
        raise ValidationError(f"Invalid default: `{default}`")

    class InitializerOptionsOrCustomDictField(fields.Field):
        def _deserialize(self, value, attr, data, **kwargs):
            if isinstance(value, str):
                if value not in initializers:
                    raise ValidationError(f"Expected one of: {initializers}, found: {value}")
                return value

            if isinstance(value, dict):
                if "type" not in value:
                    raise ValidationError("Dict must contain 'type'")
                if value["type"] not in initializers:
                    raise ValidationError(f"Dict expected key 'type' to be one of: {initializers}, found: {value}")
                return value

            raise ValidationError("Field should be str or dict")

        def _jsonschema_type_mapping(self):
            initializers = list(initializer_registry.keys())
            return {
                "oneOf": [
                    {
                        "type": ["string", "null"],
                        "enum": initializers,
                        "default": self.default,
                    },
                    # Note: default not provided in the custom dict option:
                    {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": initializers},
                        },
                        "required": ["type"],
                        "additionalProperties": True,
                    },
                ]
            }

    return field(
        metadata={"marshmallow_field": InitializerOptionsOrCustomDictField(allow_none=True, default=default)},
        default=default,
    )


def FloatRangeTupleDataclassField(N=2, default: Tuple = (0.9, 0.999), min=0, max=1):
    if N != len(default):
        raise ValidationError(f"Dimension of tuple '{N}' must match dimension of default val. '{default}'")

    class FloatTupleMarshmallowField(fields.Tuple):
        def _jsonschema_type_mapping(self):
            validate_range(default)
            return {
                "type": "array",
                "prefixItems": [
                    {
                        "type": "number",
                        "minimum": min,
                        "maximum": max,
                    }
                ]
                * N,
                "default": default,
            }

    def validate_range(data: Tuple):
        if isinstance(data, tuple) and all([isinstance(x, float) or isinstance(x, int) for x in data]):
            if all(list(map(lambda b: min <= b <= max, data))):
                return data
            raise ValidationError(
                f"Values in received tuple should be in range [{min},{max}], instead received: {data}"
            )
        raise ValidationError(f'Received value should be of {N}-dimensional "Tuple[float]", instead received: {data}')

    try:
        validate_range(default)
    except Exception:
        raise ValidationError(f"Invalid default: `{default}`")

    return field(
        metadata={
            "marshmallow_field": FloatTupleMarshmallowField(
                tuple_fields=[fields.Float()] * N, allow_none=False, validate=validate_range, default=default
            )
        },
        default=default,
    )


def IntegerOrStringOptionsField(
    options: List[str],
    nullable: bool,
    default: Union[None, int],
    # default_numeric: Union[None, int],
    # default_option: Union[None, str],
    is_integer: bool = True,
    min: Union[None, int] = None,
    max: Union[None, int] = None,
    min_exclusive: Union[None, int] = None,
    max_exclusive: Union[None, int] = None,
):
    is_integer = True
    return NumericOrStringOptionsField(**locals())


def NumericOrStringOptionsField(
    options: List[str],
    nullable: bool,
    default: Union[None, int, float, str],
    # default_numeric: Union[None, int, float],
    # default_option: Union[None, str],
    is_integer: bool = False,
    min: Union[None, int] = None,
    max: Union[None, int] = None,
    min_exclusive: Union[None, int] = None,
    max_exclusive: Union[None, int] = None,
):
    class IntegerOrStringOptionsField(fields.Field):
        def _deserialize(self, value, attr, data, **kwargs):
            msg_type = "integer" if is_integer else "numeric"
            if (is_integer and isinstance(value, int)) or isinstance(value, float):
                if (
                    (min is not None and value < min)
                    or (min_exclusive is not None and value <= min_exclusive)
                    or (max is not None and value > max)
                    or (max_exclusive is not None and value >= max_exclusive)
                ):
                    err_min_r, err_min_n = "(", min_exclusive if min_exclusive is not None else "[", min
                    errMaxR, errMaxN = ")", max_exclusive if max_exclusive is not None else "]", max
                    raise ValidationError(
                        f"If value is {msg_type} should be in range: {err_min_r}{err_min_n},{errMaxN}{errMaxR}"
                    )
                return value
            if isinstance(value, str):
                if value not in options:
                    raise ValidationError(f"String value should be one of {options}")
                return value

            raise ValidationError(f"Field should be either a {msg_type} or string")

        def _jsonschema_type_mapping(self):
            # Note: schemas can normally support a list of enums that includes 'None' as an option, as we currently have
            # in 'initializers_registry'. But to make the schema here a bit more straightforward, the user must
            # explicitly state if 'None' is going to be supported; if this conflicts with the list of enums then an
            # error is raised and if it's going to be supported then it will be as a separate subschema rather than as
            # part of the string subschema (see below):
            if None in options and not self.allow_none:
                raise AssertionError(
                    f"Provided string options `{options}` includes `None`, but field is not set to allow `None`."
                )

            # Prepare numeric option:
            numeric_type = "integer" if is_integer else "number"
            numeric_option = {"type": numeric_type}  # , "default": default_numeric}
            if min is not None:
                numeric_option["minimum"] = min
            if min_exclusive is not None:
                numeric_option["exclusiveMinimum"] = min_exclusive
            if max is not None:
                numeric_option["maximum"] = max
            if max_exclusive is not None:
                numeric_option["exclusiveMaximum"] = max_exclusive

            # Prepare string option (remove None):
            if None in options:
                options.remove(None)
            string_option = {
                "type": "string",
                "enum": options,
                # "default": default_option,
            }
            oneof_list = [
                numeric_option,
                string_option,
            ]

            # Add null as an option if applicable:
            oneof_list += [{"type": "null"}] if nullable else []

            return {"oneOf": oneof_list}

    return field(
        metadata={"marshmallow_field": IntegerOrStringOptionsField(allow_none=nullable, default=default)},
        default=default,
    )