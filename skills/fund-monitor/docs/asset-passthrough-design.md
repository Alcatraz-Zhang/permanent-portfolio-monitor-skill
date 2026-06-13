# fund-monitor 资产穿透设计 (v0.2.0)

> 状态：已与用户确认（2026-06-11）。本文件为实现依据。

## 背景与目标

用户持有的部分基金是**复合基金**（内部同时配置股/债/黄金）。仅按「基金占比」监控无法反映组合**真实的资产大类敞口**。本次升级：穿透到底层资产类别（股/债/金/现金），按**全天候目标配比**检测偏离，并给出**落到具体基金**的再平衡建议。

## 已确认决策

1. **资产类别为主**：移除每只基金的 `target_pct`，不再做基金级偏离告警。
2. **再平衡建议落到具体基金**（方案 A）：每个超阈值的资产类别，选一只「再平衡标的基金」并给出买卖金额；复合基金附副作用提示。
3. 资产类别键用英文 `stock/bond/gold/cash`，报告显示中文标签；可扩展自定义类别（原样显示）。
4. `balance`（支付宝余额）→ 计入 `cash` 类。
5. 再平衡为**一阶估算**（多类同时偏离时买卖会相互稀释）；执行后重新 `check` 收敛——报告中注明。

否决方案：B（全局最优解，需 numpy/scipy，违背最小依赖）；C（只报类别不落基金，不满足需求）。

## 配置 schema

```yaml
target_assets:        # 新增：资产类别目标配比（求和≈100）
  stock: 30
  bond:  40
  gold:  15
  cash:  15
threshold_pct: 5      # 偏离阈值（百分点），作用于资产类别
balance: 5000.0       # 支付宝余额 → 计入 cash
funds:
  - code: "000478"
    name: "建信转债增强债券A"
    shares: 12000.00
    assets: { bond: 80, stock: 20 }   # 新增：基金内部构成(%)，求和≈100
  - code: "000216"
    name: "华安黄金ETF联接A"
    shares: 3000.00
    assets: { gold: 100 }
```

`target_pct` 字段被移除（若残留则忽略，向后兼容）。

## 计算（穿透）

```
对每只基金: 市值 = 份额 × 净值
  对 assets 中每个 (类别, 比例): class_value[类别] += 市值 × 比例/100
  若 assets 求和 < 100: 差额计入 class_value["unclassified"]
  若基金未写 assets: 全部市值计入 unclassified
cash: class_value[cash] += balance
total = Σ基金市值 + balance
actual_pct[类别] = class_value[类别] / total × 100      (total=0 → 0，无除零)
deviation_pp[类别] = actual_pct[类别] − target_assets[类别]
```

类别集合 = target_assets 的键 ∪ 实际持有的类别，顺序：target_assets 顺序在前，额外类别次之，unclassified 最后。

## 再平衡到基金（方案 A）

对每个 `|deviation_pp| > threshold` 的类别 C：
- `delta_value = target_pct/100 × total − actual_value`（>0 需加仓该类，<0 需减仓）。
- C == cash：无对应基金 → 建议直接增减支付宝余额 `delta_value`。
- 否则在含该类的基金中选标的：
  - 加仓（delta>0）：选纯度最高者 `max(assets[C], 市值)`（副作用最小）。
  - 减仓（delta<0）：选该类市值贡献最大者 `max(市值 × assets[C])`（最有效）。
  - `基金买卖额 = delta_value ÷ (assets[C]/100)`。
  - 副作用：该基金其余类别 D 会被同向带动 `基金买卖额 × assets[D]/100`，在建议中提示。
- 该类无任何对应基金 → 提示「无对应基金，需新增该类资产」。

## 报告结构（check / status）

1. 头部：日期、组合总市值、支付宝余额及占比。
2. **资产配置穿透表**：`资产类别 | 目标 | 实际 | 偏离 | 市值`。
3. 有告警：`### ⚠️ 再平衡提醒`（类别级）+ `### 今日操作建议`（落到基金 + 副作用提示 + 一阶估算说明）；无告警：`✅ 组合配比正常，无需调整。`
4. `### 持仓明细`：`基金 | 代码 | 最新净值 | 份额 | 市值 | 占比 | 资产构成`（信息参考，无目标/偏离列）。

## 边界

- 未分类资产（缺 assets / 求和<100）：单列 unclassified（显示「未分类」）并在报告注明。
- 某资产类别无对应基金：再平衡建议降级为文字提示。
- total=0（空组合）：actual_pct 全 0，无除零。
- 基金净值获取失败：市值按 0 计，持仓明细标注「数据获取失败」，不拖垮报告。

## 改动与测试

- `portfolio.py`：移除 `find_rebalance_alerts` 的基金级语义；`compute_allocation` 去掉 target_pct/deviation、透传 assets；新增 `ASSET_LABELS`、`compute_asset_allocation`、`find_asset_rebalance_alerts`、`suggest_fund_trades`；重写 `format_check_markdown`、`format_status_table`；`build_allocation` 透传 assets。
- 数据/文档：更新 `data/portfolio.yaml`（加 target_assets + 每基金 assets，含 1 只复合基金）、`references/config-example.yaml`、`SKILL.md`。
- 测试（pytest，TDD）：移除过时的基金级 target/drift 用例；新增穿透计算、资产告警边界、基金映射（纯/复合/无对应/cash）、报告格式、未分类降级等用例；保持全绿。

## 成功标准

1. `python -m pytest tests/ -v` 全绿。
2. `check --format markdown` 输出穿透表 + 资产级告警 + 落到基金的操作建议（复合基金含副作用提示），中文+emoji 正常，无 traceback。
3. 复合基金穿透正确（市值按内部比例拆分汇总）。
4. cash 类与「无对应基金」类降级提示正确。
5. 仅依赖 requests+pyyaml+stdlib；pathlib 全程。
