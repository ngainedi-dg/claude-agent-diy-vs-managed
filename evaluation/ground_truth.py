"""Load the answer key and match an agent's findings against it.

Matching is deterministic. See "How the evaluator matches findings" in task/schema.md.
"""

import json
from datetime import date
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "ground_truth.json"
INJECTED_KINDS = ("injected_defect", "injected_anomaly")


def load_ground_truth(path: Path = DEFAULT_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def finding_text(finding: dict) -> str:
    """Everything the agent wrote about a finding, lowercased, for keyword matching."""
    parts = [finding.get("description", ""), finding.get("uncertainty", "")]
    for item in finding.get("evidence", []):
        parts += [str(item.get("metric", "")), str(item.get("value", "")), str(item.get("note", ""))]
    return " ".join(parts).lower()


def _date_range(finding: dict):
    try:
        start, end = finding["segment"]["date_range"]
        return date.fromisoformat(start), date.fromisoformat(end)
    except (KeyError, TypeError, ValueError):
        return None


def _segment_value_matches(actual, wanted) -> bool:
    values = actual if isinstance(actual, list) else [actual]
    return any(str(v).strip().lower() == str(wanted).strip().lower() for v in values)


def rule_matches(rule: dict, finding: dict) -> bool:
    """True if the finding satisfies every condition in the rule."""
    segment = finding.get("segment") or {}
    text = finding_text(finding)

    for key, wanted in rule.get("segment", {}).items():
        if key not in segment or not _segment_value_matches(segment[key], wanted):
            return False

    if "date_overlap" in rule or "max_span_days" in rule:
        rng = _date_range(finding)
        if rng is None:
            return False
        if "date_overlap" in rule:
            lo, hi = (date.fromisoformat(x) for x in rule["date_overlap"])
            if rng[0] > hi or rng[1] < lo:
                return False
        if "max_span_days" in rule and (rng[1] - rng[0]).days > rule["max_span_days"]:
            return False

    if "transaction_ids_overlap_min" in rule:
        listed = set(segment.get("transaction_ids") or [])
        if len(listed & set(rule["_truth_ids"])) < rule["transaction_ids_overlap_min"]:
            return False

    if not all(term in text for term in rule.get("keywords_all", [])):
        return False
    any_terms = rule.get("keywords_any", [])
    if any_terms and not any(term in text for term in any_terms):
        return False
    return True


def finding_matches(anomaly: dict, finding: dict) -> bool:
    truth_ids = anomaly.get("transaction_ids", [])
    for rule in anomaly["match"]:
        if rule_matches({**rule, "_truth_ids": truth_ids}, finding):
            return True
    return False


def score_findings(findings: list[dict], truth: dict) -> dict:
    """Match every finding against every ground-truth entry."""
    entries = truth["anomalies"]
    matches = {e["id"]: [f.get("finding_id", f"#{i}") for i, f in enumerate(findings) if finding_matches(e, f)]
               for e in entries}
    kinds = {e["id"]: e["kind"] for e in entries}

    injected = [e["id"] for e in entries if e["kind"] in INJECTED_KINDS]
    ambiguous = [e["id"] for e in entries if e["kind"] == "ambiguous"]
    legit = [e["id"] for e in entries if e["kind"] == "legitimate_pattern"]

    detected = [i for i in injected if matches[i]]
    unmatched, legit_flagged = [], []
    for n, f in enumerate(findings):
        fid = f.get("finding_id", f"#{n}")
        hit = [i for i in matches if fid in matches[i]]
        if not hit:
            unmatched.append(fid)
        elif all(kinds[i] == "legitimate_pattern" for i in hit) and f.get("further_investigation_recommended"):
            legit_flagged.append(fid)  # a genuine pattern presented as something to investigate

    return {
        "injected_total": len(injected),
        "injected_detected": len(detected),
        "injected_recall": len(detected) / len(injected) if injected else None,
        "detected": detected,
        "missed": [i for i in injected if not matches[i]],
        "ambiguous_detected": [i for i in ambiguous if matches[i]],
        "legit_patterns_mentioned": [i for i in legit if matches[i]],
        "legit_patterns_flagged_as_problems": legit_flagged,
        "unmatched_findings": unmatched,
        "matches": matches,
    }
