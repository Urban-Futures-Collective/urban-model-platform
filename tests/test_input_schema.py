"""Unit tests for the OGC inputs → JSON Schema mapper (Feature X).

Pure function, so these are table tests: no ports, no async, no I/O.
Covers the three ways providers spell "required" plus the degraded shapes a
sparse process description can produce.

See reports/REF-F10-mcp-endpoint.md.
"""

import pytest

from ump.core.utils.input_schema import is_input_required, ogc_inputs_to_json_schema


@pytest.mark.parametrize(
    "input_def, expected",
    [
        ({"required": True}, True),
        ({"required": False}, False),
        ({"schema": {"required": True}}, True),
        ({"schema": {"required": False}}, False),
        ({"minOccurs": 1}, True),
        ({"minOccurs": 0}, False),
        ({"minOccurs": "not-a-number"}, False),
        ({}, False),
        # explicit flag wins over minOccurs
        ({"required": False, "minOccurs": 1}, False),
        ({"required": True, "minOccurs": 0}, True),
    ],
)
def test_is_input_required(input_def, expected):
    assert is_input_required(input_def) is expected


def test_maps_schema_and_promotes_title_and_description():
    schema = ogc_inputs_to_json_schema(
        {
            "name": {
                "title": "Name",
                "description": "Who to greet",
                "schema": {"type": "string"},
                "minOccurs": 1,
            }
        }
    )

    assert schema["type"] == "object"
    assert schema["properties"]["name"] == {
        "type": "string",
        "title": "Name",
        "description": "Who to greet",
    }
    assert schema["required"] == ["name"]


def test_schema_own_title_is_not_overwritten():
    schema = ogc_inputs_to_json_schema(
        {
            "n": {
                "title": "outer",
                "schema": {"type": "integer", "title": "inner"},
            }
        }
    )

    assert schema["properties"]["n"]["title"] == "inner"


def test_required_key_omitted_when_nothing_is_required():
    schema = ogc_inputs_to_json_schema({"opt": {"schema": {"type": "string"}}})

    assert "required" not in schema
    assert schema["properties"]["opt"] == {"type": "string"}


def test_input_without_schema_becomes_unconstrained_property():
    schema = ogc_inputs_to_json_schema({"anything": {}})

    assert schema["properties"] == {"anything": {}}


def test_empty_and_none_inputs():
    empty = {"type": "object", "properties": {}}
    assert ogc_inputs_to_json_schema({}) == empty
    assert ogc_inputs_to_json_schema(None) == empty


def test_non_mapping_input_definition_is_skipped():
    schema = ogc_inputs_to_json_schema({"bad": "not-a-dict", "good": {}})

    assert "bad" not in schema["properties"]
    assert "good" in schema["properties"]
