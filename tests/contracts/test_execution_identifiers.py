"""The SDK-free execution boundary must agree with the public wire contract."""

import pytest
from platform_contracts.v1 import ExecutionRef
from platform_core.domain.control import ExternalObservation, RuleViolation, valid_identifier
from pydantic import ValidationError


@pytest.mark.parametrize(
    "value",
    [
        "a",
        "0",
        "A_b-9",
        "x" * 128,
        "",
        "x" * 129,
        "_x",
        "-x",
        "a/b",
        "x:y",
        "x.y",
        " x",
        "x ",
        "x\n",
        "é",
        "中",
        "１",
        "x\x00",
        None,
        1,
        True,
        [],
        {},
    ],
)
def test_identifier_grammar_matches_dto(value):
    try:
        ExecutionRef(execution_id=value)
        expected = True
    except ValidationError:
        expected = False
    assert valid_identifier(value) is expected
    for field in ["action_id", "execution_id"]:
        values = dict(
            event_id="opaque:event:1",
            action_id="action",
            revision=1,
            state="RUNNING",
            execution_id="execution",
            executed=True,
        )
        values[field] = value
        if expected:
            ExternalObservation(**values).validate()
        else:
            with pytest.raises(RuleViolation) as error:
                ExternalObservation(**values).validate()
            assert error.value.code == "INVALID_BINDING"
