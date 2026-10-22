"""Descriptive least squares only: no inference, prediction or causal claims."""

import numpy as np

from platform_operators.series import InputError


def trend(groups, minimum):
    result = []
    for group, rows in groups.items():
        if len({dt for dt, _, _ in rows}) != len(rows):
            raise InputError("Duplicate timestamps in trend input")
        valid = sorted((dt, value) for dt, value, _ in rows if value is not None)
        row = {
            "group": group,
            "sample_count": len(valid),
            "status": "EVALUABLE",
            "reason": None,
            "origin": valid[0][0].isoformat() if valid else None,
            "slope_per_day": None,
            "slope_per_year": None,
            "intercept": None,
            "r_squared": None,
        }
        if len(valid) < minimum:
            row.update(status="NOT_EVALUABLE", reason="INSUFFICIENT_DISTINCT_TIMES")
        else:
            x = np.array([(dt - valid[0][0]).total_seconds() / 86400 for dt, _ in valid])
            y = np.array([value for _, value in valid])
            if np.all(y == y[0]):
                slope, intercept, r2 = 0.0, float(y[0]), None
            else:
                # Centering improves conditioning while reporting intercept at the first time.
                center = float(x.mean())
                solution, _, rank, _ = np.linalg.lstsq(
                    np.column_stack((x - center, np.ones(len(x)))), y, rcond=None
                )
                if rank != 2:
                    raise InputError("Numerically singular design")
                slope = float(solution[0])
                intercept = float(solution[1] - slope * center)
                predicted = solution[0] * (x - center) + solution[1]
                r2 = float(1 - np.sum((y - predicted) ** 2) / np.sum((y - y.mean()) ** 2))
            numbers = [slope, slope * 365.2425, intercept] + ([] if r2 is None else [r2])
            if not all(np.isfinite(numbers)):
                raise InputError("Numerical range exceeded")
            row.update(
                slope_per_day=slope,
                slope_per_year=slope * 365.2425,
                intercept=intercept,
                r_squared=r2,
            )
        result.append(row)
    return result
