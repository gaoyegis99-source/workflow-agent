from copy import deepcopy

import pytest
from platform_checkers.reference import references
from platform_operators.cli import run
from platform_operators.series import InputError


def request(operator="statistics-linear-trend", **parameters):
    return {
        "target": {"operator_id": operator},
        "binding": {
            "selection": {
                "field_bindings": {"groupField": "f0", "timeField": "f1", "valueField": "f2"}
            },
            "semantics": {
                "timezone": "UTC",
                "missing_values": ["", "NA"],
                "value_unit": "m",
                "quantity": "water_level_elevation",
                "time_format": "ISO8601",
            },
        },
        "parameters": {
            "groups": ["A", "absent"],
            "start": "2024-01-01T00:00:00+00:00",
            "end": "2024-01-05T00:00:00+00:00",
            "frequency": "day",
            "min_samples": 3,
            **parameters,
        },
    }


def rows(values=(1, 3, 5)):
    return [
        {"f0": "A", "f1": f"2024-01-0{i + 1}T00:00:00Z", "f2": str(value)}
        for i, value in enumerate(values)
    ]


def compare(actual, expected):
    assert len(actual) == len(expected)
    for left, right in zip(actual, expected, strict=True):
        assert set(left) == set(right)
        for key, value in right.items():
            assert (
                left[key] == pytest.approx(value, abs=1e-10, rel=1e-8)
                if isinstance(value, float)
                else left[key] == value
            )


@pytest.mark.parametrize("values", [(1, 3, 5), (2, 2, 2), (1, "NA", 5), (1e8, 1e8 + 1, 1e8 + 4)])
@pytest.mark.parametrize("operator", ["timeseries-completeness", "statistics-linear-trend"])
def test_independent_reference(values, operator):
    req, data = request(operator), rows(values)
    compare(run(req, data)["rows"], references(data, req)[operator])


def test_hand_computed_and_absent_group():
    result = run(request(), rows())["rows"]
    assert result[0]["slope_per_day"] == pytest.approx(2)
    assert result[0]["slope_per_year"] == pytest.approx(730.485)
    assert result[0]["intercept"] == pytest.approx(1)
    assert result[0]["r_squared"] == pytest.approx(1)
    assert result[1]["status"] == "NOT_EVALUABLE"
    assert result[1]["slope_per_day"] is None
    result = run(request("timeseries-completeness"), rows())["rows"]
    assert result[0]["completeness"] == 0.75
    assert result[1]["missing_periods"] == 4


def test_duplicates_cover_once_but_trend_rejects():
    data = rows() + rows()[:1] + [{"f0": "A", "f1": "2024-01-04", "f2": ""}]
    result = run(request("timeseries-completeness"), data)["rows"][0]
    assert result["observed_periods"] == 3
    assert result["duplicate_periods"] == 1 and result["null_count"] == 1
    with pytest.raises(InputError):
        run(request(), data)


@pytest.mark.parametrize(
    "frequency,start,end,expected",
    [
        ("day", "2024-02-01", "2024-03-01", 29),
        ("month", "2024-01-01", "2025-01-01", 12),
        ("quarter", "2024-01-01", "2025-01-01", 4),
        ("year", "2024-01-01", "2025-01-01", 1),
    ],
)
def test_calendar_leap_and_exclusive_end(frequency, start, end, expected):
    req = request(
        "timeseries-completeness",
        frequency=frequency,
        start=start + "T00:00:00Z",
        end=end + "T00:00:00Z",
    )
    data = [{"f0": "A", "f1": end, "f2": "10"}]
    output = run(req, data)["rows"][0]
    assert output["expected_periods"] == expected and output["observed_periods"] == 0


@pytest.mark.parametrize("value", ["garbage", "inf", "-inf", "NaN", True])
def test_illegal_values(value):
    data = rows()
    data[0]["f2"] = value
    with pytest.raises(InputError):
        run(request(), data)


@pytest.mark.parametrize("time", ["garbage", "2024-11-03T01:30:00", "2024-03-10T02:30:00"])
def test_invalid_ambiguous_and_nonexistent_time(time):
    req = request(start="2024-01-01T00:00:00-05:00", end="2025-01-01T00:00:00-05:00")
    req["binding"]["semantics"]["timezone"] = "America/New_York"
    data = rows()
    data[0]["f1"] = time
    with pytest.raises(InputError):
        run(req, data)


def test_utc_elapsed_days_crosses_dst():
    req = request(start="2024-03-09T00:00:00-05:00", end="2024-03-12T00:00:00-04:00")
    req["binding"]["semantics"]["timezone"] = "America/New_York"
    data = [
        {"f0": "A", "f1": t, "f2": y}
        for t, y in zip(
            ["2024-03-09T00:00:00-05:00", "2024-03-10T00:00:00-05:00", "2024-03-11T00:00:00-04:00"],
            ["0", "24", "47"],
            strict=True,
        )
    ]
    assert run(req, data)["rows"][0]["slope_per_day"] == pytest.approx(24)
    compare(run(req, data)["rows"], references(data, req)["statistics-linear-trend"])
    req = deepcopy(req)
    req["parameters"]["start"] = "2024-03-09T01:00:00-05:00"
    with pytest.raises(InputError):
        run(req, data)
