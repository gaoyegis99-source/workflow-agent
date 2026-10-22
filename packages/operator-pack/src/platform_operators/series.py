"""Explicit monitoring-series semantics, without inference or silent repair."""

from collections import Counter
from datetime import UTC, datetime
from math import isfinite
from zoneinfo import ZoneInfo


class InputError(ValueError):
    pass


def moment(value, zone):
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if dt.tzinfo is not None:
            return dt.astimezone(UTC)
        alternatives = {
            dt.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            for fold in (0, 1)
            if dt.replace(tzinfo=zone, fold=fold)
            .astimezone(UTC)
            .astimezone(zone)
            .replace(tzinfo=None)
            == dt
        }
        if len(alternatives) != 1:
            raise InputError("Ambiguous or nonexistent local time")
        return alternatives.pop()
    except (ValueError, TypeError, OverflowError) as exc:
        raise InputError("Invalid or ambiguous time") from exc


def period(dt, frequency):
    if frequency == "day":
        return dt.date().toordinal()
    if frequency == "month":
        return dt.year * 12 + dt.month - 1
    if frequency == "quarter":
        return dt.year * 4 + (dt.month - 1) // 3
    if frequency == "year":
        return dt.year
    raise InputError("Unsupported frequency")


def prepare(rows, binding, parameters, semantics):
    zone = ZoneInfo(semantics["timezone"])
    start, end = (moment(parameters[k], zone).astimezone(zone) for k in ("start", "end"))
    frequency = parameters["frequency"]
    for dt in (start, end):
        if (
            dt.hour
            or dt.minute
            or dt.second
            or dt.microsecond
            or frequency != "day"
            and dt.day != 1
            or frequency == "quarter"
            and dt.month not in (1, 4, 7, 10)
            or frequency == "year"
            and dt.month != 1
        ):
            raise InputError("Calendar range is not aligned to its frequency")
    expected = period(end, frequency) - period(start, frequency)
    if expected <= 0:
        raise InputError("Empty analysis range")
    groups = {name: [] for name in parameters["groups"]}
    for row in rows:
        group = row[binding["groupField"]]
        if not isinstance(group, str):
            raise InputError("Group identifiers must have explicit string type")
        if group not in groups:
            continue
        time = moment(row[binding["timeField"]], zone)
        value = row[binding["valueField"]]
        if value is None or isinstance(value, str) and value in semantics["missing_values"]:
            number = None
        else:
            if isinstance(value, bool):
                raise InputError("Boolean is not a numerical observation")
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise InputError("Non-numeric observation") from exc
            if not isfinite(number):
                raise InputError("Non-finite observation")
        if start.astimezone(UTC) <= time < end.astimezone(UTC):
            groups[group].append((time, number, period(time.astimezone(zone), frequency)))
    return groups, expected


def completeness(groups, expected):
    output = []
    for group, rows in groups.items():
        counts = Counter(p for _, _, p in rows)
        observed = len({p for _, value, p in rows if value is not None})
        output.append(
            {
                "group": group,
                "expected_periods": expected,
                "observed_periods": observed,
                "missing_periods": expected - observed,
                "duplicate_periods": sum(count > 1 for count in counts.values()),
                "null_count": sum(value is None for _, value, _ in rows),
                "completeness": observed / expected,
            }
        )
    return output
