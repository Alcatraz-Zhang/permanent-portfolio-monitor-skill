"""Offline test suite for scripts/portfolio.py (permanent-portfolio-monitor).

ALL network access is mocked via unittest.mock.patch. These tests never touch
the real fundgz / eastmoney endpoints. Run from the skill root:

    python -m pytest tests/ -v

v0.2.0: asset-class look-through model. Drift is detected at the asset-class
level (stock/bond/gold/cash); rebalance suggestions map back to concrete funds.
"""

import os
from unittest import mock

import pytest
import requests

import portfolio


# ---------------------------------------------------------------------------
# Mock samples (test data only — never used by the implementation directly)
# ---------------------------------------------------------------------------
FUNDGZ_BODY = (
    'jsonpgz({"fundcode":"110011","name":"易方达优质精选混合",'
    '"jzrq":"2026-06-09","dwjz":"3.4000","gsz":"3.4560",'
    '"gszzl":"1.65","gztime":"2026-06-10 15:00"});'
)

EASTMONEY_JSON = {
    "Data": {
        "LSJZList": [
            {"FSRQ": "2026-06-09", "DWJZ": "3.4000", "LJJZ": "4.1200", "JZZZL": "-0.32"}
        ]
    },
    "ErrCode": 0,
    "ErrMsg": "",
}


def _mock_response(text=None, json_data=None):
    resp = mock.Mock()
    if text is not None:
        resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


def _fund(code, shares, nav, assets, name=None, error=None, change_pct=None):
    """A per-fund row shaped like compute_allocation output (with market_value)."""
    mv = (shares * nav) if nav is not None else 0.0
    return {
        "code": code, "name": name or code, "shares": shares, "nav": nav,
        "market_value": mv, "actual_pct": 0.0, "assets": assets,
        "change_pct": change_pct, "error": error,
    }


def _portfolio(fund_specs, balance, target):
    """Build (allocation, asset_allocation) from simple specs via real functions.

    fund_specs: list of (code, name, shares, nav, assets-dict).
    """
    funds = [
        {"code": c, "name": n, "shares": sh, "nav": nv, "assets": a,
         "change_pct": None, "quote_error": None}
        for (c, n, sh, nv, a) in fund_specs
    ]
    alloc = portfolio.compute_allocation(funds, balance)
    aa = portfolio.compute_asset_allocation(alloc["rows"], alloc["balance"], target)
    return alloc, aa


# ---------------------------------------------------------------------------
# fetch_fundgz  (S9)
# ---------------------------------------------------------------------------
def test_fetch_fundgz_parses_body():
    with mock.patch("portfolio.requests.get", return_value=_mock_response(text=FUNDGZ_BODY)):
        data = portfolio.fetch_fundgz("110011")
    assert data["dwjz"] == "3.4000"
    assert data["gsz"] == "3.4560"
    assert data["name"] == "易方达优质精选混合"


def test_fetch_fundgz_garbage_returns_none():
    with mock.patch("portfolio.requests.get", return_value=_mock_response(text="garbage not jsonp")):
        assert portfolio.fetch_fundgz("110011") is None


def test_fetch_fundgz_timeout_returns_none():
    with mock.patch("portfolio.requests.get", side_effect=requests.Timeout("timed out")):
        assert portfolio.fetch_fundgz("110011") is None


# ---------------------------------------------------------------------------
# fetch_eastmoney  (P2)
# ---------------------------------------------------------------------------
def test_fetch_eastmoney_returns_first_record_and_sends_referer():
    with mock.patch(
        "portfolio.requests.get", return_value=_mock_response(json_data=EASTMONEY_JSON)
    ) as mget:
        rec = portfolio.fetch_eastmoney("110011")
    assert rec["DWJZ"] == "3.4000"
    assert mget.call_args.kwargs["headers"]["Referer"] == "https://fundf10.eastmoney.com/"


def test_fetch_eastmoney_errcode_nonzero_returns_none():
    bad = {"Data": {"LSJZList": [{"DWJZ": "1.0"}]}, "ErrCode": 1, "ErrMsg": "err"}
    with mock.patch("portfolio.requests.get", return_value=_mock_response(json_data=bad)):
        assert portfolio.fetch_eastmoney("110011") is None


def test_fetch_eastmoney_empty_list_returns_none():
    empty = {"Data": {"LSJZList": []}, "ErrCode": 0, "ErrMsg": ""}
    with mock.patch("portfolio.requests.get", return_value=_mock_response(json_data=empty)):
        assert portfolio.fetch_eastmoney("110011") is None


def test_fetch_eastmoney_request_exception_returns_none():
    with mock.patch("portfolio.requests.get", side_effect=requests.RequestException("boom")):
        assert portfolio.fetch_eastmoney("110011") is None


# ---------------------------------------------------------------------------
# get_quote  (S1 / S7 / fallback / S5)
# ---------------------------------------------------------------------------
def test_get_quote_uses_gsz_estimate():  # S1
    raw = {"name": "易方达", "dwjz": "3.4000", "gsz": "3.4560",
           "gszzl": "1.65", "jzrq": "2026-06-09", "gztime": "2026-06-10 15:00"}
    with mock.patch("portfolio.fetch_fundgz", return_value=raw):
        q = portfolio.get_quote("110011")
    assert q["nav"] == 3.4560
    assert q["source"] == "fundgz"
    assert q["estimate"] is True


def test_get_quote_falls_back_to_dwjz_when_gsz_empty():  # S7
    raw = {"name": "x", "dwjz": "3.4000", "gsz": "", "gszzl": "", "jzrq": "2026-06-09"}
    with mock.patch("portfolio.fetch_fundgz", return_value=raw):
        q = portfolio.get_quote("110011")
    assert q["nav"] == 3.4000
    assert q["estimate"] is False


def test_get_quote_eastmoney_fallback():
    em = {"FSRQ": "2026-06-09", "DWJZ": "3.4000", "JZZZL": "-0.32"}
    with mock.patch("portfolio.fetch_fundgz", return_value=None), \
         mock.patch("portfolio.fetch_eastmoney", return_value=em):
        q = portfolio.get_quote("110011")
    assert q["nav"] == 3.4000
    assert q["source"] == "eastmoney"


def test_get_quote_both_fail_returns_error():  # S5
    with mock.patch("portfolio.fetch_fundgz", return_value=None), \
         mock.patch("portfolio.fetch_eastmoney", return_value=None):
        q = portfolio.get_quote("000000")
    assert "error" in q and "nav" not in q


# ---------------------------------------------------------------------------
# resolve_threshold (S8) + format_quote
# ---------------------------------------------------------------------------
def test_resolve_threshold_precedence():  # S8
    assert portfolio.resolve_threshold(1, {"threshold_pct": 5}) == 1
    assert portfolio.resolve_threshold(None, {"threshold_pct": 7}) == 7
    assert portfolio.resolve_threshold(None, {}) == 5


def test_format_quote_success_and_error():
    q = {"code": "110011", "name": "易方达", "nav": 3.4560, "prev_nav": 3.4000,
         "change_pct": 1.65, "source": "fundgz", "time": "t", "estimate": True}
    text = portfolio.format_quote(q)
    assert "易方达" in text and "3.4560" in text and "+1.65%" in text
    assert portfolio.format_quote({"code": "000000", "error": "数据获取失败"}) == "000000: 数据获取失败"


# ---------------------------------------------------------------------------
# compute_allocation  (per-fund market value; no fund-level target/deviation)
# ---------------------------------------------------------------------------
def test_compute_allocation_basic_and_zero():
    funds = [{"code": "a", "name": "A", "shares": 100, "nav": 2.0, "assets": {"stock": 100}}]
    alloc = portfolio.compute_allocation(funds, 800)
    row = alloc["rows"][0]
    assert row["market_value"] == 200.0
    assert alloc["total"] == 1000.0
    assert round(row["actual_pct"], 1) == 20.0
    assert row["assets"] == {"stock": 100}            # assets passthrough
    assert "deviation_pp" not in row                   # fund-level drift removed
    # zero-holdings: no ZeroDivisionError
    assert portfolio.compute_allocation([], 0)["total"] == 0
    z = portfolio.compute_allocation([{"code": "a", "name": "A", "shares": 0, "nav": 1.0, "assets": {}}], 0)
    assert z["rows"][0]["actual_pct"] == 0.0


def test_compute_allocation_nav_none_marks_error():
    funds = [{"code": "x", "name": "X", "shares": 100, "nav": None,
              "assets": {"stock": 100}, "quote_error": "数据获取失败"}]
    alloc = portfolio.compute_allocation(funds, 0)
    assert alloc["rows"][0]["market_value"] == 0.0
    assert alloc["rows"][0]["error"]


# ---------------------------------------------------------------------------
# compute_asset_allocation  (look-through)
# ---------------------------------------------------------------------------
def test_compute_asset_allocation_passthrough():
    rows = [
        {"code": "S", "name": "S", "market_value": 40000, "assets": {"stock": 100}, "error": None},
        {"code": "M", "name": "M", "market_value": 50000, "assets": {"bond": 80, "stock": 20}, "error": None},
        {"code": "G", "name": "G", "market_value": 10000, "assets": {"gold": 100}, "error": None},
    ]
    target = {"stock": 30, "bond": 40, "gold": 15, "cash": 15}
    aa = portfolio.compute_asset_allocation(rows, 20000, target)
    by = {r["class"]: r for r in aa["rows"]}
    assert aa["total"] == 120000
    assert round(by["stock"]["value"]) == 50000       # 40000 + 50000*0.2
    assert round(by["bond"]["value"]) == 40000
    assert by["cash"]["value"] == 20000               # from balance
    assert round(by["stock"]["actual_pct"], 1) == 41.7
    assert round(by["stock"]["deviation_pp"], 1) == 11.7
    assert by["stock"]["label"] == "股票"
    assert [r["class"] for r in aa["rows"]][:4] == ["stock", "bond", "gold", "cash"]  # target order


def test_compute_asset_allocation_unclassified():
    rows = [
        {"code": "X", "name": "X", "market_value": 10000, "assets": {}, "error": None},        # no assets
        {"code": "Y", "name": "Y", "market_value": 10000, "assets": {"stock": 60}, "error": None},  # sum<100
    ]
    aa = portfolio.compute_asset_allocation(rows, 0, {"stock": 50})
    by = {r["class"]: r for r in aa["rows"]}
    assert round(by["stock"]["value"]) == 6000
    assert round(by["unclassified"]["value"]) == 14000   # 10000 + 10000*0.4
    assert aa["rows"][-1]["class"] == "unclassified"      # unclassified last


def test_compute_asset_allocation_zero_total():
    aa = portfolio.compute_asset_allocation([], 0, {"stock": 50, "cash": 50})
    assert aa["total"] == 0
    assert all(r["actual_pct"] == 0.0 for r in aa["rows"])


# ---------------------------------------------------------------------------
# find_asset_rebalance_alerts
# ---------------------------------------------------------------------------
def test_find_asset_rebalance_alerts_threshold_and_delta():
    aa = {"total": 100000, "rows": [
        {"class": "stock", "label": "股票", "target_pct": 30, "actual_pct": 45, "value": 45000, "deviation_pp": 15},
        {"class": "bond", "label": "债券", "target_pct": 40, "actual_pct": 35, "value": 35000, "deviation_pp": -5},  # boundary
        {"class": "gold", "label": "黄金", "target_pct": 15, "actual_pct": 8, "value": 8000, "deviation_pp": -7},
    ]}
    alerts = portfolio.find_asset_rebalance_alerts(aa, 5)
    assert {a["class"] for a in alerts} == {"stock", "gold"}   # boundary (==5) excluded
    stock = next(a for a in alerts if a["class"] == "stock")
    gold = next(a for a in alerts if a["class"] == "gold")
    assert stock["delta_value"] < 0   # over target -> reduce (30000-45000)
    assert gold["delta_value"] > 0    # under target -> add (15000-8000)


# ---------------------------------------------------------------------------
# suggest_fund_trades  (pure / composite / cash / no-fund)
# ---------------------------------------------------------------------------
def test_suggest_fund_trades_pure_fund():
    alerts = [{"class": "gold", "label": "黄金", "deviation_pp": -7,
               "actual_pct": 8, "target_pct": 15, "delta_value": 7000}]
    rows = [_fund("000216", 1, 8000, {"gold": 100}, name="华安黄金"),
            _fund("S", 1, 45000, {"stock": 100}, name="股基")]
    sug = portfolio.suggest_fund_trades(alerts, rows)[0]
    assert sug["fund_code"] == "000216"
    assert sug["action"] == "加仓"
    assert round(sug["trade_value"]) == 7000
    assert not sug["side_effects"]


def test_suggest_fund_trades_composite_side_effects():
    alerts = [{"class": "bond", "label": "债券", "deviation_pp": -8,
               "actual_pct": 32, "target_pct": 40, "delta_value": 8000}]
    rows = [_fund("000478", 1, 50000, {"bond": 80, "stock": 20}, name="建信转债")]
    sug = portfolio.suggest_fund_trades(alerts, rows)[0]
    assert sug["fund_code"] == "000478"
    assert round(sug["trade_value"]) == 10000          # 8000 / 0.8
    assert any("股票" in se for se in sug["side_effects"])


def test_suggest_fund_trades_cash_has_no_fund():
    alerts = [{"class": "cash", "label": "现金", "deviation_pp": -11,
               "actual_pct": 4, "target_pct": 15, "delta_value": 11000}]
    rows = [_fund("S", 1, 45000, {"stock": 100}, name="股基")]
    sug = portfolio.suggest_fund_trades(alerts, rows)[0]
    assert sug.get("fund_code") is None
    assert "余额" in sug["note"] or "现金" in sug["note"]


def test_suggest_fund_trades_no_fund_for_class():
    alerts = [{"class": "gold", "label": "黄金", "deviation_pp": -15,
               "actual_pct": 0, "target_pct": 15, "delta_value": 15000}]
    rows = [_fund("S", 1, 45000, {"stock": 100}, name="股基")]
    sug = portfolio.suggest_fund_trades(alerts, rows)[0]
    assert sug.get("fund_code") is None
    assert "新增" in sug["note"] or "无" in sug["note"]


# ---------------------------------------------------------------------------
# format_check_markdown  (new asset-class structure)
# ---------------------------------------------------------------------------
def test_format_check_markdown_with_alerts():
    alloc, aa = _portfolio(
        [("000216", "华安黄金", 5000, 2.0, {"gold": 100}), ("S", "股基", 10000, 3.0, {"stock": 100})],
        1000, {"stock": 30, "gold": 40, "cash": 30})
    alerts = portfolio.find_asset_rebalance_alerts(aa, 5)
    sug = portfolio.suggest_fund_trades(alerts, alloc["rows"])
    md = portfolio.format_check_markdown(alloc, aa, alerts, sug, 5)
    assert "资产配置穿透" in md
    assert "| 资产类别 | 目标 | 实际 | 偏离 | 市值 |" in md
    assert "### ⚠️ 再平衡提醒" in md
    assert "### 今日操作建议" in md
    assert "持仓明细" in md


def test_format_check_markdown_no_alerts():
    alloc, aa = _portfolio([("S", "股基", 10000, 3.0, {"stock": 100})], 0, {"stock": 100})
    md = portfolio.format_check_markdown(alloc, aa, [], [], 999)
    assert "✅ 组合配比正常，无需调整。" in md
    assert "资产配置穿透" in md
    assert "持仓明细" in md


# ---------------------------------------------------------------------------
# config I/O  (S11)
# ---------------------------------------------------------------------------
def test_load_config_path_precedence(tmp_path):  # S11
    p1 = tmp_path / "a.yaml"
    p1.write_text("threshold_pct: 3\nbalance: 100\nfunds: []\n", encoding="utf-8")
    p2 = tmp_path / "b.yaml"
    p2.write_text("threshold_pct: 9\nbalance: 200\nfunds: []\n", encoding="utf-8")
    assert portfolio.load_config(str(p1))["threshold_pct"] == 3
    with mock.patch.dict(os.environ, {"PERMANENT_PORTFOLIO_MONITOR_CONFIG": str(p2)}):
        assert portfolio.load_config()["threshold_pct"] == 9
        assert portfolio.load_config(str(p1))["threshold_pct"] == 3


def test_save_config_roundtrip_unicode(tmp_path):
    p = tmp_path / "c.yaml"
    cfg = {"threshold_pct": 5, "balance": 1000.0, "target_assets": {"stock": 50, "cash": 50},
           "funds": [{"code": "110011", "name": "易方达优质精选", "shares": 12.5, "assets": {"stock": 100}}]}
    portfolio.save_config(cfg, str(p))
    reloaded = portfolio.load_config(str(p))
    assert reloaded["funds"][0]["name"] == "易方达优质精选"
    assert reloaded["funds"][0]["assets"] == {"stock": 100}
    assert reloaded["target_assets"] == {"stock": 50, "cash": 50}


# ---------------------------------------------------------------------------
# main / CLI  (S12 update persistence, S10 UTF-8)
# ---------------------------------------------------------------------------
def _write_cfg(path):
    path.write_text(
        "target_assets: {stock: 50, cash: 50}\nthreshold_pct: 5\nbalance: 0\nfunds:\n"
        "- {code: '110011', name: 易方达, shares: 1, assets: {stock: 100}}\n",
        encoding="utf-8",
    )


def test_main_update_balance_persists(tmp_path):  # S12
    p = tmp_path / "d.yaml"
    _write_cfg(p)
    assert portfolio.main(["update", "--balance", "8000", "--config", str(p)]) == 0
    assert portfolio.load_config(str(p))["balance"] == 8000.0


def test_main_update_shares_persists(tmp_path):  # S12 / S4
    p = tmp_path / "e.yaml"
    _write_cfg(p)
    assert portfolio.main(["update", "110011", "--shares", "1500", "--config", str(p)]) == 0
    assert portfolio.load_config(str(p))["funds"][0]["shares"] == 1500.0


def test_main_update_shares_without_code_errors(tmp_path):  # S12
    p = tmp_path / "f.yaml"
    p.write_text("balance: 0\nfunds: []\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        portfolio.main(["update", "--shares", "1500", "--config", str(p)])


def test_main_check_utf8_no_error(capsys):  # S10
    fake_quote = {"code": "110011", "name": "易方达", "nav": 3.4, "prev_nav": 3.4,
                  "change_pct": 1.0, "source": "fundgz", "time": "t", "estimate": True}
    fake_cfg = {"threshold_pct": 5, "balance": 1000.0, "target_assets": {"stock": 50, "cash": 50},
                "funds": [{"code": "110011", "name": "易方达", "shares": 100, "assets": {"stock": 100}}]}
    with mock.patch("portfolio.get_quote", return_value=fake_quote), \
         mock.patch("portfolio.load_config", return_value=fake_cfg):
        rc = portfolio.main(["check", "--threshold", "999", "--format", "markdown"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "✅ 组合配比正常，无需调整。" in out
    assert "组合监控报告" in out
