#!/usr/bin/env python3
# generate_report.py
import json
import sys
from datetime import datetime


def generate_report(complaints: list, output_format: str = "simple") -> dict:
    """生成投诉处理报告"""

    total = len(complaints)
    escalated = sum(1 for c in complaints if c.get("escalate", False))
    high_priority = sum(1 for c in complaints if c.get("priority") == "high")

    # 统计触发的规则
    rule_counts = {}
    for c in complaints:
        for rule in c.get("matched_rules", []):
            rule_counts[rule] = rule_counts.get(rule, 0) + 1

    report = {
        "report_id": f"RPT-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total_complaints": total,
            "escalated": escalated,
            "escalation_rate": f"{escalated / total * 100:.1f}%" if total > 0 else "0%",
            "high_priority": high_priority
        },
        "rule_trigger_summary": rule_counts,
        "details": complaints if output_format == "detailed" else []
    }

    return report


if __name__ == "__main__":
    data = json.loads(sys.stdin.read())
    complaints = data.get("complaints", [])
    output_format = data.get("output_format", "simple")

    result = generate_report(complaints, output_format)
    print(json.dumps(result, ensure_ascii=False, indent=2))