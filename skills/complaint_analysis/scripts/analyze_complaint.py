# !/usr/bin/env python3
# analyze_complaint.py
import json
import sys


def analyze_complaint(amount: int, customer_level: str, days: int, history_count: int) -> dict:
    """投诉分析核心逻辑"""

    result = {
        "escalate": False,
        "priority": "low",
        "suggestion": "",
        "matched_rules": []
    }

    # 规则1：金额阈值
    if amount > 10000:
        result["matched_rules"].append("R001")
        result["escalate"] = True
        result["priority"] = "high"
        result["suggestion"] = "投诉金额超过1万元，建议立即升级至部门经理处理"
    elif amount > 5000:
        result["matched_rules"].append("R002")
        result["priority"] = "medium"
        result["suggestion"] = "投诉金额较高，建议优先处理，2小时内回复"
    else:
        result["suggestion"] = "常规投诉，按正常流程处理"

    # 规则2：VIP客户加权
    if customer_level == "VIP" and amount > 5000:
        result["matched_rules"].append("R003")
        result["escalate"] = True
        result["priority"] = "high"
        result["suggestion"] = "VIP客户投诉金额超过5000元，触发快速通道"

    # 规则3：超时处理
    if days > 7:
        result["matched_rules"].append("R004")
        result["escalate"] = True
        result["priority"] = "high"
        result["suggestion"] = result["suggestion"] + "；投诉已超时7天，自动升级"
    elif days > 3:
        result["matched_rules"].append("R005")
        result["priority"] = "medium"
        result["suggestion"] = result["suggestion"] + "；投诉已超3天，建议加急处理"

    # 规则4：历史投诉频发
    if history_count >= 3:
        result["matched_rules"].append("R006")
        result["priority"] = "high"
        result["suggestion"] = result["suggestion"] + "；近30天投诉≥3次，触发特别关注"

    return result


if __name__ == "__main__":
    # 从命令行参数或标准输入读取
    if len(sys.argv) >= 5:
        amount = int(sys.argv[1])
        customer_level = sys.argv[2]
        days = int(sys.argv[3])
        history_count = int(sys.argv[4])
    else:
        # 从stdin读取JSON
        data = json.loads(sys.stdin.read())
        amount = data.get("amount", 0)
        customer_level = data.get("customer_level", "Normal")
        days = data.get("days", 0)
        history_count = data.get("history_count", 0)

    result = analyze_complaint(amount, customer_level, days, history_count)
    print(json.dumps(result, ensure_ascii=False))