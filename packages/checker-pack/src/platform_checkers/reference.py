"""Independent reference calculation. No production parser or numerical code is imported."""

from datetime import UTC, datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo


def timestamp(raw, timezone):
    parsed = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    if parsed.tzinfo is None:
        candidates = []
        for fold in range(2):
            candidate = parsed.replace(tzinfo=timezone, fold=fold).astimezone(UTC)
            if candidate.astimezone(timezone).replace(tzinfo=None) == parsed:
                candidates.append(candidate)
        if not candidates or len(set(candidates)) > 1:
            raise ValueError("Invalid local time")
        parsed = candidates[0]
    return parsed.astimezone(UTC)


def index(instant, frequency):
    return {
        "day": instant.date().toordinal(),
        "month": instant.year * 12 + instant.month - 1,
        "quarter": instant.year * 4 + (instant.month - 1) // 3,
        "year": instant.year,
    }[frequency]


def references(rows, request):
    semantics = request["binding"]["semantics"]
    fields = request["binding"]["selection"]["field_bindings"]
    args = request["parameters"]
    timezone = ZoneInfo(semantics["timezone"])
    start, end = [timestamp(args[key], timezone) for key in ("start", "end")]
    for dt in [start.astimezone(timezone), end.astimezone(timezone)]:
        if (
            dt.time().isoformat() != "00:00:00"
            or args["frequency"] in ["month", "quarter", "year"]
            and dt.day != 1
            or args["frequency"] == "quarter"
            and (dt.month - 1) % 3
            or args["frequency"] == "year"
            and dt.month != 1
        ):
            raise ValueError("Unaligned calendar boundary")
    total = index(end.astimezone(timezone), args["frequency"]) - index(
        start.astimezone(timezone), args["frequency"]
    )
    groups = {key: [] for key in args["groups"]}
    for row in rows:
        key = row[fields["groupField"]]
        if key not in groups:
            continue
        dt = timestamp(row[fields["timeField"]], timezone)
        raw = row[fields["valueField"]]
        number = (
            None
            if raw is None or isinstance(raw, str) and raw in semantics["missing_values"]
            else Decimal(str(raw))
        )
        if number is not None and not number.is_finite():
            raise ValueError("Non-finite input")
        if start <= dt < end:
            groups[key].append((dt, number))
    completeness, trends = [], []
    for key, entries in groups.items():
        buckets = {}
        for dt, number in entries:
            bucket = index(dt.astimezone(timezone), args["frequency"])
            buckets.setdefault(bucket, []).append(number)
        found = sum(any(value is not None for value in values) for values in buckets.values())
        completeness.append(
            {
                "group": key,
                "expected_periods": total,
                "observed_periods": found,
                "missing_periods": total - found,
                "duplicate_periods": sum(len(values) > 1 for values in buckets.values()),
                "null_count": sum(value is None for _, value in entries),
                "completeness": float(Decimal(found) / Decimal(total)),
            }
        )
        if len({dt for dt, _ in entries}) != len(entries):
            trends.append({"group": key, "invalid": "DUPLICATE_TIMESTAMPS"})
            continue
        valid = sorted((dt, number) for dt, number in entries if number is not None)
        result = {
            "group": key,
            "sample_count": len(valid),
            "status": "NOT_EVALUABLE",
            "reason": "INSUFFICIENT_DISTINCT_TIMES",
            "origin": valid[0][0].isoformat() if valid else None,
            "slope_per_day": None,
            "slope_per_year": None,
            "intercept": None,
            "r_squared": None,
        }
        if len(valid) >= args["min_samples"]:
            with localcontext() as context:
                context.prec = 60
                x = [
                    Decimal((dt - valid[0][0]).days)
                    + Decimal((dt - valid[0][0]).seconds) / 86400
                    + Decimal((dt - valid[0][0]).microseconds) / 86400000000
                    for dt, _ in valid
                ]
                y = [number for _, number in valid]
                n = Decimal(len(x))
                mx, my = sum(x) / n, sum(y) / n
                slope = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / sum(
                    (a - mx) ** 2 for a in x
                )
                intercept = my - slope * mx
                variance = sum((b - my) ** 2 for b in y)
                r2 = (
                    None
                    if variance == 0
                    else 1
                    - sum((b - intercept - slope * a) ** 2 for a, b in zip(x, y, strict=True))
                    / variance
                )
                result.update(
                    status="EVALUABLE",
                    reason=None,
                    slope_per_day=float(slope),
                    slope_per_year=float(slope * Decimal("365.2425")),
                    intercept=float(intercept),
                    r_squared=float(r2) if r2 is not None else None,
                )
        trends.append(result)
    return {"timeseries-completeness": completeness, "statistics-linear-trend": trends}
