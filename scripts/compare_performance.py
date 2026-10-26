"""Compare release benchmark reports with a curated relative baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def value_at_path(data, path):
    value = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(path)
        value = value[part]
    return float(value)


def compare_reports(baseline, reports):
    failures = []
    results = []
    for metric in baseline.get("metrics", []):
        section = str(metric["section"])
        path = str(metric["path"])
        baseline_value = float(metric["baseline"])
        ratio = float(metric.get("max_ratio", 1.75))
        slack = float(metric.get("slack", 5.0))
        try:
            current = value_at_path(reports[section], path)
        except KeyError as exc:
            print(f"Missing key in {section}: {exc}")
            failures.append(f"{section}: missing key {exc}")
            continue
        allowed = max(baseline_value * ratio, baseline_value + slack)
        result = {
            "metric": f"{section}.{path}",
            "baseline": baseline_value,
            "current": current,
            "allowed": allowed,
            "ok": current <= allowed,
        }
        results.append(result)
        if not result["ok"]:
            failures.append(result)
    return {"ok": not failures, "results": results, "failures": failures}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument(
        "--report",
        action="append",
        default=[],
        help="SECTION=JSON_PATH; may be repeated",
    )
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    reports = {}
    for item in args.report:
        section, separator, path = item.partition("=")
        if not separator or not section or not path:
            raise SystemExit(f"Invalid --report value: {item}")
        reports[section] = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = sorted(
        {str(metric["section"]) for metric in baseline.get("metrics", [])}
        - reports.keys()
    )
    if missing:
        raise SystemExit(f"Missing benchmark report sections: {missing}")
    result = compare_reports(baseline, reports)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit("Performance regression threshold exceeded")


if __name__ == "__main__":
    main()
