---
name: complaint_analysis
description: 分析客户投诉数据，根据规则给出处理建议，并生成结构化报告。
---

## 包含脚本

### analyze_complaint.py — 分析单条投诉

**调用方式**（二选一）：

```bash
# 方式 1（推荐）：stdin 喂 JSON
echo '{"amount": 15000, "customer_level": "VIP", "days": 10, "history_count": 4}' \
  | python scripts/analyze_complaint.py

# 方式 2：位置参数（没有 --flag，全是位置参数）
python scripts/analyze_complaint.py 15000 VIP 10 4
```

**输入字段**（JSON / 位置参数同名）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `amount` | int | 投诉金额（元） |
| `customer_level` | str | `VIP` 或 `Normal` |
| `days` | int | 已处理天数 |
| `history_count` | int | 近 30 天投诉次数 |

**输出**（stdout，单行 JSON）：

```json
{
  "escalate": true,
  "priority": "high",
  "suggestion": "VIP客户投诉金额超过5000元，触发快速通道；投诉已超时7天，自动升级；近30天投诉≥3次，触发特别关注",
  "matched_rules": ["R001", "R003", "R004", "R006"]
}
```

### generate_report.py — 生成批量报告

**调用方式**（仅 stdin）：

```bash
echo '{"complaints": [<analyze_complaint 的输出列表>], "output_format": "detailed"}' \
  | python scripts/generate_report.py
```

**输入**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `complaints` | list | 每条都是 `analyze_complaint.py` 输出的对象 |
| `output_format` | str | `simple`（默认）或 `detailed` |

**输出**（stdout，pretty JSON）：

```json
{
  "report_id": "RPT-20260915021458",
  "generated_at": "2026-09-15T02:14:58",
  "summary": {"total_complaints": 4, "escalated": 2, "escalation_rate": "50.0%", "high_priority": 2},
  "rule_trigger_summary": {"R001": 2, "R003": 2, ...},
  "details": [...]
}
```

## 推荐工作流程

1. 用 `Skill` viewer 读本文件，拿到脚本接口
2. 对每条投诉跑 `analyze_complaint.py`，把结果收集成 list
3. 把 list 喂给 `generate_report.py` 输出结构化报告
4. 根据报告里的 `priority` / `escalate` / `suggestion` 字段给用户写处理建议