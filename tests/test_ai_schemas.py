from linkedin_automation.ai import ADMIN_INTENT_SCHEMA, CONTENT_SCHEMA


def test_admin_intent_strict_schema_requires_all_schedule_fields():
    required = set(ADMIN_INTENT_SCHEMA["required"])

    assert {"frequency", "weekdays", "hour", "minute", "until"} <= required
    assert ADMIN_INTENT_SCHEMA["additionalProperties"] is False


def test_admin_intent_time_fields_are_required_but_nullable_and_bounded():
    properties = ADMIN_INTENT_SCHEMA["properties"]

    assert properties["hour"] == {
        "type": ["integer", "null"],
        "minimum": 0,
        "maximum": 23,
    }
    assert properties["minute"] == {
        "type": ["integer", "null"],
        "minimum": 0,
        "maximum": 59,
    }
    assert properties["until"]["type"] == ["string", "null"]
    assert properties["weekdays"]["items"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 6,
    }
    assert properties["weekdays"]["maxItems"] == 7


def test_content_schema_reserves_room_for_bounded_hashtags():
    properties = CONTENT_SCHEMA["properties"]

    assert properties["post"]["maxLength"] == 2800
    assert properties["hashtags"]["maxItems"] == 5
    assert properties["topic"]["maxLength"] == 240
