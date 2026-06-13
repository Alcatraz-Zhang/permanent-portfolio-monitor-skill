#!/usr/bin/env python3
"""permanent-portfolio-monitor — 基金组合资产穿透监控 CLI.

Subcommands: status | quote | update | check

净值数据来源:
  * fundgz (天天基金实时估值): http://fundgz.1234567.com.cn/js/<code>.js
  * eastmoney (历史净值 LSJZ):  https://api.fund.eastmoney.com/f10/lsjz

v0.2.0 资产穿透模型: 每只基金按其内部 assets 比例 (股/债/金/...) 拆分市值,
汇总成整个组合真实的资产类别敞口, 对照 target_assets 检测偏离, 并把再平衡
建议映射回具体基金。所有网络调用集中在 fetch_fundgz / fetch_eastmoney; 纯函数层
(compute_* / find_* / suggest_* / format_*) 完全离线, 可直接单元测试。
"""

import argparse
import datetime
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

import requests
import yaml

FUNDGZ_URL = "http://fundgz.1234567.com.cn/js/{code}.js"
EASTMONEY_URL = (
    "https://api.fund.eastmoney.com/f10/lsjz"
    "?fundCode={code}&pageIndex=1&pageSize=1"
)
EASTMONEY_REFERER = "https://fundf10.eastmoney.com/"
DEFAULT_THRESHOLD = 5
CASH_CLASS = "cash"
UNCLASSIFIED = "unclassified"
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "portfolio.yaml"

# 资产类别 -> 中文显示标签 (未列出的自定义类别原样显示)
ASSET_LABELS = {
    "stock": "股票",
    "bond": "债券",
    "gold": "黄金",
    "cash": "现金",
    "commodity": "商品",
    "reit": "REITs",
    UNCLASSIFIED: "未分类",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _to_float(value):
    """Best-effort float conversion; return None for empty / non-numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _money(value):
    """Format a CNY amount with thousands separators, no decimals: ¥1,523."""
    return f"¥{value:,.0f}"


def _label(cls):
    """Display label for an asset class key."""
    return ASSET_LABELS.get(cls, cls)


def _vwidth(text):
    """Visual width of a string (CJK wide chars count as 2 columns)."""
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text, width, align="left"):
    """Pad text to a given visual width, honoring CJK double-width chars."""
    text = str(text)
    fill = max(0, width - _vwidth(text))
    return (text + " " * fill) if align == "left" else (" " * fill + text)


def _render_table(headers, rows, aligns=None):
    """Render an aligned plain-text table (CJK-width aware)."""
    aligns = aligns or ["left"] * len(headers)
    grid = [headers] + rows
    widths = [max(_vwidth(r[i]) for r in grid) for i in range(len(headers))]

    def render(cells):
        return "  ".join(_pad(c, widths[i], aligns[i]) for i, c in enumerate(cells))

    out = [render(headers), "  ".join("-" * w for w in widths)]
    out += [render(r) for r in rows]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Network parsers (the only functions that touch the network)
# ---------------------------------------------------------------------------
def fetch_fundgz(code, timeout=10):
    """Fetch the realtime estimate from fundgz; return the raw dict or None."""
    url = FUNDGZ_URL.format(code=code)
    try:
        resp = requests.get(url, timeout=timeout)
        text = resp.text
        match = re.search(r"jsonpgz\((.*)\);?", text, re.S)
        if match is None:
            return None
        return json.loads(match.group(1))  # ValueError (JSONDecodeError) on bad body
    except (requests.RequestException, ValueError, AttributeError, KeyError):
        return None


def fetch_eastmoney(code, timeout=10):
    """Fetch the latest historical NAV record from eastmoney; dict or None.

    Sends ``Referer: https://fundf10.eastmoney.com/`` (required by the API).
    Returns None when ErrCode != 0, the LSJZList is empty, or on any error.
    """
    url = EASTMONEY_URL.format(code=code)
    headers = {"Referer": EASTMONEY_REFERER}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        data = resp.json()
        if data.get("ErrCode") != 0:
            return None
        records = data.get("Data", {}).get("LSJZList") or []
        if not records:
            return None
        return records[0]
    except (requests.RequestException, ValueError, AttributeError, KeyError, TypeError):
        return None


def get_quote(code):
    """Resolve a normalized quote for one fund code.

    Returns ``{code, name, nav, prev_nav, change_pct, source, time, estimate}``
    on success, or ``{code, error}`` when no data can be fetched.

    Precedence: fundgz 估算净值(gsz) -> fundgz 单位净值(dwjz) -> eastmoney 历史净值.
    """
    raw = fetch_fundgz(code)
    if raw is not None:
        name = raw.get("name", "")
        gsz_f = _to_float(raw.get("gsz"))
        dwjz_f = _to_float(raw.get("dwjz"))
        if gsz_f not in (None, 0.0):
            return {
                "code": code, "name": name, "nav": gsz_f, "prev_nav": dwjz_f,
                "change_pct": _to_float(raw.get("gszzl")), "source": "fundgz",
                "time": raw.get("gztime") or raw.get("jzrq"), "estimate": True,
            }
        if dwjz_f not in (None, 0.0):
            return {
                "code": code, "name": name, "nav": dwjz_f, "prev_nav": dwjz_f,
                "change_pct": None, "source": "fundgz",
                "time": raw.get("jzrq"), "estimate": False,
            }
    em = fetch_eastmoney(code)
    if em is not None:
        dwjz_f = _to_float(em.get("DWJZ"))
        if dwjz_f is not None:
            return {
                "code": code, "name": "", "nav": dwjz_f, "prev_nav": dwjz_f,
                "change_pct": _to_float(em.get("JZZZL")), "source": "eastmoney",
                "time": em.get("FSRQ"), "estimate": False,
            }
    return {"code": code, "error": "数据获取失败"}


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------
def _resolve_config_path(path=None):
    """Resolve the config path: explicit arg > env var > default data file."""
    if path:
        return Path(path)
    env = os.environ.get("PERMANENT_PORTFOLIO_MONITOR_CONFIG")
    if env:
        return Path(env)
    return DATA_PATH


def load_config(path=None):
    """Load the portfolio config (YAML) into a dict with safe defaults."""
    cfg_path = _resolve_config_path(path)
    with open(cfg_path, "r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
    cfg.setdefault("funds", [])
    cfg.setdefault("balance", 0.0)
    cfg.setdefault("target_assets", {})
    return cfg


def save_config(cfg, path=None):
    """Persist the config back to YAML (unicode preserved, insertion order)."""
    cfg_path = _resolve_config_path(path)
    with open(cfg_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(cfg, handle, allow_unicode=True, sort_keys=False)


def resolve_threshold(cli_threshold, cfg):
    """Threshold precedence: --threshold flag > config threshold_pct > 5."""
    if cli_threshold is not None:
        return cli_threshold
    configured = cfg.get("threshold_pct")
    if configured is not None:
        return configured
    return DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# Pure computation layer (no network)
# ---------------------------------------------------------------------------
def compute_allocation(funds, balance):
    """Compute per-fund market values and each fund's share of the portfolio.

    ``funds`` is a list of config-shaped dicts each augmented with a resolved
    ``nav`` (float or None), ``assets`` (class->pct), and optional ``change_pct``
    / ``quote_error``. Pure function — never hits the network.

    Returns ``{rows, funds_value, balance, total, balance_pct}``. ``total == 0``
    yields ``actual_pct == 0.0`` (no ZeroDivisionError). No fund-level target /
    deviation here — drift lives at the asset-class layer (compute_asset_allocation).
    """
    rows = []
    funds_value = 0.0
    for fund in funds:
        nav = fund.get("nav")
        shares = _to_float(fund.get("shares")) or 0.0
        if nav is None:
            market_value = 0.0
            error = fund.get("quote_error") or "数据获取失败"
        else:
            market_value = shares * float(nav)
            error = None
        funds_value += market_value
        rows.append({
            "code": fund.get("code"),
            "name": fund.get("name", ""),
            "shares": shares,
            "nav": float(nav) if nav is not None else None,
            "change_pct": fund.get("change_pct"),
            "assets": fund.get("assets") or {},
            "market_value": market_value,
            "actual_pct": 0.0,
            "error": error,
        })

    balance = _to_float(balance) or 0.0
    total = funds_value + balance
    for row in rows:
        row["actual_pct"] = (row["market_value"] / total * 100) if total > 0 else 0.0
    balance_pct = (balance / total * 100) if total > 0 else 0.0

    return {
        "rows": rows,
        "funds_value": funds_value,
        "balance": balance,
        "total": total,
        "balance_pct": balance_pct,
    }


def compute_asset_allocation(rows, balance, target_assets, cash_class=CASH_CLASS):
    """Look-through: split each fund's market value by its asset composition and
    aggregate into true portfolio asset-class exposure.

    ``rows`` are per-fund rows from compute_allocation (carry market_value + assets).
    ``balance`` (支付宝余额) is added to the cash class. A fund whose ``assets`` sum
    to < 100 contributes the remainder to ``unclassified``; a fund with no assets
    contributes its whole value to ``unclassified``.

    Returns ``{rows: [{class,label,target_pct,actual_pct,value,deviation_pp}],
    total, class_value}``. Class order: target_assets order, then extra held
    classes, then unclassified last. ``total == 0`` -> all actual_pct 0.0.
    """
    class_value = {}
    for row in rows:
        market_value = row.get("market_value", 0.0) or 0.0
        if market_value <= 0:
            continue
        assets = row.get("assets") or {}
        classified = 0.0
        for cls, pct in assets.items():
            weight = _to_float(pct) or 0.0
            if weight <= 0:
                continue
            class_value[cls] = class_value.get(cls, 0.0) + market_value * weight / 100
            classified += weight
        if classified < 100:
            remainder = 100 - classified
            class_value[UNCLASSIFIED] = (
                class_value.get(UNCLASSIFIED, 0.0) + market_value * remainder / 100
            )

    balance = _to_float(balance) or 0.0
    if balance:
        class_value[cash_class] = class_value.get(cash_class, 0.0) + balance

    funds_value = sum((r.get("market_value", 0.0) or 0.0) for r in rows)
    total = funds_value + balance

    target_assets = target_assets or {}
    ordered = [c for c in target_assets]
    for cls in class_value:
        if cls not in ordered and cls != UNCLASSIFIED:
            ordered.append(cls)
    if class_value.get(UNCLASSIFIED) and UNCLASSIFIED not in ordered:
        ordered.append(UNCLASSIFIED)

    out_rows = []
    for cls in ordered:
        value = class_value.get(cls, 0.0)
        actual_pct = (value / total * 100) if total > 0 else 0.0
        target_pct = _to_float(target_assets.get(cls)) or 0.0
        out_rows.append({
            "class": cls,
            "label": _label(cls),
            "target_pct": target_pct,
            "actual_pct": actual_pct,
            "value": value,
            "deviation_pp": actual_pct - target_pct,
        })

    return {"rows": out_rows, "total": total, "class_value": class_value}


def find_asset_rebalance_alerts(asset_allocation, threshold):
    """Return asset-class rows whose |deviation_pp| strictly exceeds threshold.

    Each alert carries ``delta_value = target_value - actual_value``:
      * > 0  -> 需加仓该类 (under target)
      * < 0  -> 需减仓该类 (over target)
    Deviation exactly equal to the threshold is NOT an alert (strict >).
    """
    total = asset_allocation["total"]
    alerts = []
    for row in asset_allocation["rows"]:
        if abs(row["deviation_pp"]) > threshold:
            target_value = row["target_pct"] / 100 * total
            alert = dict(row)
            alert["delta_value"] = target_value - row["value"]
            alerts.append(alert)
    return alerts


def suggest_fund_trades(alerts, rows, cash_class=CASH_CLASS):
    """Map each asset-class alert to a concrete fund buy/sell suggestion.

    For each off-target class C:
      * cash -> no fund; adjust 支付宝余额 directly.
      * else pick a rebalance fund among funds holding C (with a valid nav):
          - 加仓 (delta>0): purest holder  max(assets[C], market_value)
          - 减仓 (delta<0): largest C-value holder  max(market_value*assets[C])
        trade_value = delta_value / (assets[C]/100); side_effects note the other
        classes the trade also moves (composite funds).
      * no fund holds C -> degraded text note (需新增该类资产).

    First-order estimate (simultaneous moves dilute one another); re-run check
    to converge.
    """
    suggestions = []
    for alert in alerts:
        cls = alert["class"]
        label = alert["label"]
        delta = alert["delta_value"]
        action = "加仓" if delta > 0 else "减仓"

        if cls == cash_class:
            suggestions.append({
                "class": cls, "label": label, "delta_value": delta,
                "action": "增加现金" if delta > 0 else "减少现金",
                "note": "调整支付宝余额（流动性现金），无需买卖基金",
            })
            continue

        candidates = [
            r for r in rows
            if r.get("error") is None and (_to_float((r.get("assets") or {}).get(cls)) or 0) > 0
        ]
        if not candidates:
            suggestions.append({
                "class": cls, "label": label, "delta_value": delta, "action": action,
                "note": f"组合中暂无含{label}的基金，需新增该类资产",
            })
            continue

        if delta > 0:
            target = max(candidates, key=lambda r: (
                _to_float(r["assets"].get(cls)) or 0, r.get("market_value", 0.0)))
        else:
            target = max(candidates, key=lambda r: (
                (r.get("market_value", 0.0) or 0.0) * (_to_float(r["assets"].get(cls)) or 0)))

        concentration = (_to_float(target["assets"].get(cls)) or 0) / 100
        trade_value = delta / concentration if concentration else 0.0
        side_effects = []
        for other_cls, other_pct in (target.get("assets") or {}).items():
            if other_cls == cls:
                continue
            weight = _to_float(other_pct) or 0
            if weight > 0:
                se_value = abs(trade_value) * weight / 100
                se_dir = "增" if trade_value > 0 else "减"
                side_effects.append(f"{se_dir}{_label(other_cls)}约{_money(se_value)}")

        suggestions.append({
            "class": cls, "label": label, "delta_value": delta,
            "fund_code": target["code"], "fund_name": target.get("name") or target["code"],
            "trade_value": trade_value, "action": action, "side_effects": side_effects,
        })
    return suggestions


def build_allocation(cfg):
    """Fetch quotes for every fund in the config and compute per-fund allocation."""
    funds = []
    for fund in cfg.get("funds", []):
        quote = get_quote(fund.get("code"))
        funds.append({
            "code": fund.get("code"),
            "name": fund.get("name") or quote.get("name", ""),
            "shares": fund.get("shares", 0),
            "nav": quote.get("nav"),
            "change_pct": quote.get("change_pct"),
            "assets": fund.get("assets") or {},
            "quote_error": quote.get("error"),
        })
    return compute_allocation(funds, cfg.get("balance", 0) or 0)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------
def format_quote(quote):
    """Human-readable single-fund quote."""
    if quote.get("error"):
        return f"{quote['code']}: {quote['error']}"
    label = "估算" if quote.get("estimate") else "净值"
    lines = [f"{quote.get('name', '') or quote['code']}（{quote['code']}）"]
    lines.append(f"  最新净值: {quote['nav']:.4f}  [{label} · 来源 {quote['source']}]")
    if quote.get("change_pct") is not None:
        lines.append(f"  估算涨幅: {quote['change_pct']:+.2f}%")
    if quote.get("estimate") and quote.get("prev_nav") is not None:
        lines.append(f"  前收净值: {quote['prev_nav']:.4f}")
    if quote.get("time"):
        lines.append(f"  时间: {quote['time']}")
    return "\n".join(lines)


def _asset_table_rows(asset_allocation):
    rows = []
    for r in asset_allocation["rows"]:
        target = f"{r['target_pct']:.0f}%" if r["target_pct"] else "—"
        rows.append([r["label"], target, f"{r['actual_pct']:.1f}%",
                     f"{r['deviation_pp']:+.1f}%", _money(r["value"])])
    return rows


def _holding_table_rows(allocation):
    rows = []
    for r in allocation["rows"]:
        nav = f"{r['nav']:.4f}" if r["nav"] is not None else "—"
        market = _money(r["market_value"]) if r["error"] is None else "数据获取失败"
        comp = "、".join(
            f"{_label(k)}{_to_float(v) or 0:.0f}%" for k, v in (r.get("assets") or {}).items()
        ) or "未分类"
        rows.append([r["name"] or "", str(r["code"]), nav,
                     f"{r['shares']:.2f}", market, f"{r['actual_pct']:.1f}%", comp])
    return rows


def _suggestion_lines(suggestions):
    lines = []
    for s in suggestions:
        if s.get("fund_code"):
            line = (f"- {s['action']} **{s['fund_name']}**（{s['fund_code']}）"
                    f"约 {_money(abs(s['trade_value']))}")
            if s.get("side_effects"):
                line += f"；注意会同时{'、'.join(s['side_effects'])}"
            lines.append(line)
        else:
            lines.append(f"- {s['label']}：{s['note']}（约 {_money(abs(s['delta_value']))}）")
    return lines


def format_check_markdown(allocation, asset_allocation, alerts, suggestions, threshold):
    """Structured Markdown rebalance report (the cron-facing output)."""
    today = datetime.date.today().isoformat()
    out = [
        f"## 📊 组合监控报告 — {today}",
        "",
        f"**组合总市值：** {_money(allocation['total'])}",
        f"**支付宝余额：** {_money(allocation['balance'])}（{allocation['balance_pct']:.1f}%）",
        "",
        "### 资产配置穿透",
        "",
        "| 资产类别 | 目标 | 实际 | 偏离 | 市值 |",
        "| ---- | ---- | ---- | ---- | ---- |",
    ]
    for cells in _asset_table_rows(asset_allocation):
        out.append("| " + " | ".join(cells) + " |")
    out.append("")

    if alerts:
        out.append("### ⚠️ 再平衡提醒")
        out.append("")
        for alert in alerts:
            state = "超配" if alert["deviation_pp"] > 0 else "低配"
            need = "需减仓" if alert["delta_value"] < 0 else "需加仓"
            out.append(
                f"- {alert['label']} 实际 {alert['actual_pct']:.1f}%，{state} "
                f"{alert['deviation_pp']:+.1f}%（超过 {threshold}% 阈值，{need} "
                f"{_money(abs(alert['delta_value']))}）"
            )
        out.append("")
        out.append("### 今日操作建议")
        out.append("")
        out += _suggestion_lines(suggestions)
        out.append("")
        out.append("> 注：建议为一阶估算，执行后重新运行 check 可逐步收敛。")
    else:
        out.append("✅ 组合配比正常，无需调整。")

    out.append("")
    out.append("### 持仓明细")
    out.append("")
    out.append("| 基金 | 代码 | 最新净值 | 份额 | 市值 | 占比 | 资产构成 |")
    out.append("| ---- | ---- | -------- | ---- | ---- | ---- | -------- |")
    for cells in _holding_table_rows(allocation):
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def format_status_table(allocation, asset_allocation):
    """Aligned human-readable status: asset look-through + holdings."""
    asset_headers = ["资产类别", "目标", "实际", "偏离", "市值"]
    asset_aligns = ["left", "right", "right", "right", "right"]
    holding_headers = ["基金", "代码", "最新净值", "份额", "市值", "占比", "资产构成"]
    holding_aligns = ["left", "left", "right", "right", "right", "right", "left"]

    parts = ["【资产配置穿透】",
             _render_table(asset_headers, _asset_table_rows(asset_allocation), asset_aligns),
             "",
             "【持仓明细】",
             _render_table(holding_headers, _holding_table_rows(allocation), holding_aligns),
             "",
             f"组合总市值: {_money(allocation['total'])}   "
             f"持仓: {_money(allocation['funds_value'])}   "
             f"支付宝余额: {_money(allocation['balance'])}（{allocation['balance_pct']:.1f}%）"]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _reconfigure_utf8():
    """Force UTF-8 on stdout/stderr (Windows console defaults to GBK)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def cmd_status(args):
    cfg = load_config(args.config)
    if not cfg.get("funds"):
        print("组合为空，请先在 data/portfolio.yaml 中添加基金。")
        return 0
    allocation = build_allocation(cfg)
    asset_allocation = compute_asset_allocation(
        allocation["rows"], allocation["balance"], cfg.get("target_assets") or {})
    print(format_status_table(allocation, asset_allocation))
    return 0


def cmd_quote(args):
    print(format_quote(get_quote(args.code)))
    return 0


def cmd_update(args):
    cfg = load_config(args.config)
    changes = []
    if args.balance is not None:
        cfg["balance"] = float(args.balance)
        changes.append(f"支付宝余额 = {cfg['balance']:.2f}")
    if args.shares is not None:
        matched = False
        for fund in cfg.get("funds", []):
            if str(fund.get("code")) == str(args.code):
                fund["shares"] = float(args.shares)
                matched = True
                changes.append(f"{args.code} 份额 = {fund['shares']:.2f}")
        if not matched:
            print(f"未找到基金代码 {args.code}，请检查 data/portfolio.yaml。", file=sys.stderr)
            return 1
    save_config(cfg, args.config)
    print("已更新: " + "; ".join(changes))
    return 0


def cmd_check(args):
    cfg = load_config(args.config)
    threshold = resolve_threshold(args.threshold, cfg)
    allocation = build_allocation(cfg)
    asset_allocation = compute_asset_allocation(
        allocation["rows"], allocation["balance"], cfg.get("target_assets") or {})
    alerts = find_asset_rebalance_alerts(asset_allocation, threshold)
    suggestions = suggest_fund_trades(alerts, allocation["rows"])
    if args.format == "markdown":
        print(format_check_markdown(allocation, asset_allocation, alerts, suggestions, threshold))
    else:
        print(format_status_table(allocation, asset_allocation))
        if alerts:
            print("\n⚠️ 再平衡提醒:")
            for line in _suggestion_lines(suggestions):
                print("  " + line.lstrip("- "))
        else:
            print("\n✅ 组合配比正常，无需调整。")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="portfolio.py",
        description="permanent-portfolio-monitor: 支付宝全天候基金组合的资产穿透与配比监控",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="查看组合整体状态（资产穿透 + 持仓）")
    p_status.add_argument("--config", default=None, help="配置文件路径")

    p_quote = sub.add_parser("quote", help="查询单只基金最新净值")
    p_quote.add_argument("code", help="基金代码")
    p_quote.add_argument("--config", default=None)

    p_update = sub.add_parser("update", help="更新持有份额或支付宝余额")
    p_update.add_argument("code", nargs="?", default=None, help="基金代码（更新份额时必填）")
    p_update.add_argument("--shares", type=float, default=None, help="新的持有份额")
    p_update.add_argument("--balance", type=float, default=None, help="新的支付宝余额（元）")
    p_update.add_argument("--config", default=None)

    p_check = sub.add_parser("check", help="检测资产类别偏离并输出报告")
    p_check.add_argument("--threshold", type=float, default=None, help="偏离阈值（百分点）")
    p_check.add_argument("--format", default="markdown", choices=["markdown", "text"])
    p_check.add_argument("--config", default=None)

    return parser


def main(argv=None):
    _reconfigure_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "update":
        if args.shares is None and args.balance is None:
            parser.error("update 需要 --shares 或 --balance 之一")
        if args.shares is not None and not args.code:
            parser.error("update --shares 需要指定基金代码，例如: update 110011 --shares 1234.56")

    handlers = {
        "status": cmd_status,
        "quote": cmd_quote,
        "update": cmd_update,
        "check": cmd_check,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
