---
name: fund-monitor
description: Use when monitoring Alipay (支付宝) mutual fund portfolios, checking real-time NAV/valuation (基金净值/估值), and tracking TRUE asset-class exposure via look-through (股/债/金/现金 穿透) for all-weather or permanent portfolios (全天候/永久组合). Decomposes composite funds into stock/bond/gold/cash, detects asset-class allocation drift against targets, and emits rebalancing alerts mapped to concrete fund buy/sell actions (再平衡提醒). Uses Tiantian Fund (天天基金) and Eastmoney (东方财富) public data; supports manual holding updates and automated cron health checks.
---

# Fund Monitor

## 概述
`fund-monitor` 监控支付宝（Alipay）持仓基金组合。支付宝无公开交易 API，故手动维护持仓份额，结合天天基金/东方财富公开净值接口，实现：组合市值实时估算、**资产类别穿透**（把每只基金按其内部 股/债/金 构成拆分汇总）、对照目标资产配比的偏离检测，以及**落到具体基金**的再平衡建议。

## 何时使用
- **实时估值查询**：盘中查看单只基金估算涨跌幅（gszzl）。
- **资产穿透检查**：查看组合穿透后真实的 股/债/金/现金 敞口及与目标的偏离。
- **持仓/余额更新**：买卖或资金划转后同步份额或流动性现金。
- **自动化监控**：cron 定时检测，资产类别偏离超阈值时告警并给出基金操作建议。

## 快速参考 (CLI)
所有命令默认读取 `data/portfolio.yaml`，可用 `--config <path>` 或环境变量 `FUND_MONITOR_CONFIG` 覆盖。

### 1. 组合状态（资产穿透 + 持仓）
```bash
python scripts/portfolio.py status
```

### 2. 查询单只基金
```bash
python scripts/portfolio.py quote 110011
```

### 3. 更新持仓份额
```bash
python scripts/portfolio.py update 110011 --shares 1234.56
```

### 4. 更新支付宝余额
```bash
python scripts/portfolio.py update --balance 50000
```

### 5. 偏离检测与报告（cron 调用）
```bash
python scripts/portfolio.py check --threshold 5 --format markdown
```
**`check` 报告结构：**
- **头部**：组合总市值、支付宝余额及占比、日期。
- **资产配置穿透表**：`资产类别 | 目标 | 实际 | 偏离 | 市值`。
- **⚠️ 再平衡提醒**（资产类别级）：哪个大类超配/低配、需加/减多少。
- **今日操作建议**（落到基金）：具体买卖哪只基金、约多少钱；复合基金附副作用提示。
- **持仓明细**：`基金 | 代码 | 最新净值 | 份额 | 市值 | 占比 | 资产构成`。
- **正常状态**：所有偏离在阈值内 → `✅ 组合配比正常，无需调整。`

## 配置说明
配置位于 `data/portfolio.yaml`，首次使用请参考 `references/config-example.yaml`。

**核心字段：**
- `target_assets`：资产类别目标配比（%），如 `{stock: 30, bond: 40, gold: 15, cash: 15}`，求和≈100。键用英文（报告显示中文），可自定义类别。
- `threshold_pct`：资产类别偏离阈值（百分点）。
- `balance`：支付宝流动性现金（元），自动计入 `cash` 类。
- `funds[].assets`：每只基金内部资产构成（%），求和≈100。复合基金多项（如 `{bond: 80, stock: 20}`），纯基金单项（如 `{gold: 100}`）。

> 偏离 = 实际占比 − 目标占比（百分点）；`|偏离| > 阈值` 触发提醒。阈值优先级 `--threshold` > `threshold_pct` > 5。

## 核心逻辑
- **穿透计算**：基金市值 × 内部 `assets` 比例 → 各资产类别 → 全组合汇总（balance 计入 cash）→ 真实占比 vs `target_assets`。
- **再平衡到基金**：每个超阈值类别选一只「再平衡标的」（含该类、加仓选纯度最高/减仓选该类市值最大者），给出买卖金额；复合基金提示副作用（如「加仓会同时增股票」）。某类无对应基金 → 提示需新增。一阶估算，执行后重新 `check` 收敛。
- **未分类**：基金缺 `assets` 或求和 < 100 → 差额归「未分类」并在穿透表列出。
- **货币基金**：货基常无估值，回退用净值（约 1.0）；建议标 `{cash: 100}` 计入现金。
- **编码**：强制 UTF-8 输出，确保 Windows 下中文 + emoji 正常显示。

## 自动化监控 (Hermes Cron)
建议在 Hermes 中配置定时任务，仅在需要操作时通知用户。

**调度模板：**
```bash
# 每交易日 14:45 执行检测
python scripts/portfolio.py check --threshold 5 --format markdown
```

**通知规则：**
- 输出 **不包含** `✅ 组合配比正常，无需调整。`（即存在 ⚠️ 告警）→ 通过 QQ 通知用户。
- 包含该字符串 → 静默处理。

## 常见问题
- **数据延迟**：天天基金估值接口在非交易时段可能返回收盘净值。
- **QDII 基金**：净值通常延迟一个交易日，系统自动回退历史净值。
- **资产比例从哪来**：参考基金招募说明书/定期报告中的资产配置；估算填写即可，穿透按比例拆分市值。
