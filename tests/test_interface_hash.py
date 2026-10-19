"""Interface hash (REF-F12): text edits keep it, interface changes alter it."""

import copy

from ump.core.models.registry import interface_sha256

DESCRIPTION = {
    "id": "echo",
    "title": "Echo",
    "description": "Returns its input",
    "version": "1.0.0",
    "links": [{"href": "http://x/processes/echo", "rel": "self"}],
    "jobControlOptions": ["async-execute"],
    "outputTransmission": ["value"],
    "inputs": {
        "message": {
            "title": "Message",
            "description": "Text to echo",
            "minOccurs": 1,
            "schema": {"type": "string", "default": "hi"},
        },
        "options": {
            "title": "Options",
            "schema": {
                "type": "object",
                "properties": {
                    # a property *named* description must stay in the hash
                    "description": {"type": "string", "title": "Description"},
                    "count": {"type": "integer"},
                },
            },
        },
    },
    "outputs": {"echo": {"title": "Echo", "schema": {"type": "string"}}},
}


def _changed(mutate):
    d = copy.deepcopy(DESCRIPTION)
    mutate(d)
    return interface_sha256(d)


BASE = interface_sha256(DESCRIPTION)


def test_hash_is_hex_sha256():
    assert len(BASE) == 64 and int(BASE, 16) >= 0


def test_text_links_metadata_and_version_do_not_change_hash():
    def edit(d):
        d["title"] = "Echo v2"
        d["description"] = "new text"
        d["version"] = "2.0.0"
        d["links"] = []
        d["inputs"]["message"]["title"] = "Msg"
        d["inputs"]["message"]["description"] = "other"
        d["inputs"]["message"]["metadata"] = [{"title": "m"}]
        d["inputs"]["options"]["schema"]["properties"]["description"]["title"] = "D"

    assert _changed(edit) == BASE


def test_key_order_is_irrelevant():
    def reorder(d):
        d["inputs"] = dict(reversed(list(d["inputs"].items())))

    assert _changed(reorder) == BASE


def test_interface_changes_alter_hash():
    def added_input(d):
        d["inputs"]["extra"] = {"schema": {"type": "number"}}

    def min_occurs(d):
        d["inputs"]["message"]["minOccurs"] = 0

    def schema_type(d):
        d["inputs"]["message"]["schema"]["type"] = "number"

    def removed_named_property(d):
        del d["inputs"]["options"]["schema"]["properties"]["description"]

    def execution_mode(d):
        d["jobControlOptions"] = ["sync-execute"]

    def default_value(d):
        d["inputs"]["message"]["schema"]["default"] = "bye"

    for mutate in (
        added_input,
        min_occurs,
        schema_type,
        removed_named_property,
        execution_mode,
        default_value,
    ):
        assert _changed(mutate) != BASE, mutate.__name__
