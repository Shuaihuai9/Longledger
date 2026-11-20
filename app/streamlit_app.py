"""长衡 LongLedger · Streamlit 界面。

界面层只调用 services，不直接触碰核心引擎与数据库。
多账户：主区域每个账户一个 Tab，账户内再分功能子 Tab；
侧边栏负责账户新建/删除与全局交易参数。
启动：streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import tempfile
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import streamlit as st

from config.settings import FeeConfig
from core.money import yuan_to_cents, yuan_to_mills
from data.db import open_database
from ingest.actions_csv import load_actions_csv
from ingest.quotes_csv import import_quotes_csv
from ingest.quotes_download import download_history, split_symbols
from services.accounts import AccountService
from services.corporate_actions_service import CorporateActionService
from services.ledger import LedgerService
from services.quotes_service import get_daily_ohlc
from services.settlement import settle_day

st.set_page_config(page_title="长衡", layout="wide")


@st.cache_resource
def get_connection():
    return open_database()


def yuan(cents: int) -> float:
    """仅用于展示转换，账本内部始终是整数分。"""
    return cents / 100


def flash(kind: str, message: str) -> None:
    """跨 st.rerun() 传递提示，避免操作成功后消息被刷新吞掉。"""
    st.session_state["_flash"] = (kind, message)


def show_flash() -> None:
    if "_flash" in st.session_state:
        kind, message = st.session_state["_flash"]
        del st.session_state["_flash"]
        getattr(st, kind)(message)


conn = get_connection()
account_service = AccountService(conn)


# ---------------- 侧边栏：账户管理 + 全局交易参数 ----------------
with st.sidebar:
    st.header("账户管理")
    accounts = account_service.list_accounts_with_summary()
    st.caption(f"共 {len(accounts)} 个账户，每个账户拥有独立的现金、批次、流水与净值。")
    for row in accounts:
        st.caption(
            f"#{row['account_id']} {row['name']} · "
            f"现金 ¥{yuan(row['cash_cents']):,.2f} · {row['trade_count']} 笔交易"
        )

    with st.form("create_account_form"):
        new_name = st.text_input("账户名称", placeholder="例如：ETF 网格模拟")
        new_initial = st.number_input(
            "初始本金 (元)",
            min_value=0.0,
            value=100000.0,
            step=1000.0,
            format="%.2f",
        )
        if st.form_submit_button("新建账户"):
            try:
                account_service.create(new_name, yuan_to_cents(str(new_initial)))
                flash("success", f"账户「{new_name.strip()}」已创建，可在主区域切换。")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    if len(accounts) > 1:
        with st.form("delete_account_form"):
            options = {
                f"{row['name']} (#{row['account_id']})": row["account_id"]
                for row in accounts
            }
            target_label = st.selectbox("要删除的账户", list(options))
            confirmed = st.checkbox("确认删除该账户及其全部流水（不可恢复）")
            if st.form_submit_button("删除账户"):
                if not confirmed:
                    st.error("请先勾选删除确认")
                else:
                    try:
                        account_service.delete(options[target_label])
                        flash("success", f"账户「{target_label}」已删除。")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

    st.divider()
    st.header("交易参数")
    st.caption("全账户共享，保存后立即生效，仅影响后续成交；历史流水保留当时费率。")
    current = account_service.current_fee_config()
    st.caption(
        f"当前：佣金 {current.commission_rate * 100:.4f}% · "
        f"最低 {current.min_commission_cents / 100:.2f} 元 · "
        f"印花税 {current.stamp_tax_rate * 100:.4f}% · "
        f"过户费 {current.transfer_fee_rate * 100:.5f}%"
    )

    with st.form("fee_config_form"):
        commission_pct = st.number_input(
            "佣金费率 (%)",
            min_value=0.0,
            max_value=1.0,
            value=float(current.commission_rate * 100),
            step=0.001,
            format="%.4f",
        )
        min_commission_yuan = st.number_input(
            "最低佣金 (元)",
            min_value=0.0,
            max_value=100.0,
            value=current.min_commission_cents / 100,
            step=1.0,
            format="%.2f",
        )
        stamp_pct = st.number_input(
            "印花税率 (%)，仅卖出",
            min_value=0.0,
            max_value=1.0,
            value=float(current.stamp_tax_rate * 100),
            step=0.01,
            format="%.4f",
        )
        transfer_pct = st.number_input(
            "过户费率 (%)，双边",
            min_value=0.0,
            max_value=1.0,
            value=float(current.transfer_fee_rate * 100),
            step=0.0001,
            format="%.5f",
        )
        etf_stamp = st.checkbox("ETF 卖出免印花税", value=current.etf_stamp_tax_exempt)
        etf_transfer = st.checkbox("ETF 免过户费", value=current.etf_transfer_fee_exempt)

        if st.form_submit_button("保存交易参数"):
            try:
                account_service.update_fee_config(
                    FeeConfig(
                        commission_rate=Decimal(str(commission_pct)) / 100,
                        min_commission_cents=yuan_to_cents(str(min_commission_yuan)),
                        stamp_tax_rate=Decimal(str(stamp_pct)) / 100,
                        transfer_fee_rate=Decimal(str(transfer_pct)) / 100,
                        etf_stamp_tax_exempt=etf_stamp,
                        etf_transfer_fee_exempt=etf_transfer,
                    )
                )
                flash("success", "交易参数已保存并生效")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


def render_account(account_id: int) -> None:
    """渲染单个账户的完整视图；所有控件 key 带账户后缀，避免多账户冲突。"""
    service = LedgerService(conn, account_id)
    repo = service.repo
    pf = service.portfolio
    tag = f"a{account_id}"

    symbols = [s for s, book in pf.books.items() if book.total_qty > 0]
    prices = {}
    for symbol in symbols:
        price = repo.latest_price(symbol, date.today())
        if price:
            prices[symbol] = price

    col1, col2, col3 = st.columns(3)
    col1.metric("现金", f"¥{yuan(pf.cash_cents):,.2f}")
    col2.metric("持仓市值", f"¥{yuan(pf.market_value_cents(prices)):,.2f}")
    col3.metric("总资产", f"¥{yuan(pf.total_asset_cents(prices)):,.2f}")

    with st.expander("账户设置（初始本金 / 入金出金）"):
        st.caption(
            "无交易流水时，修改初始本金会同步调整期初现金；已有交易流水时仅调整"
            "净值/收益率基准，现金与历史流水不动。每次修改都会清空本账户历史净值快照。"
        )
        with st.form(f"initial_form_{tag}"):
            new_initial_yuan = st.number_input(
                "初始本金 (元)",
                min_value=0.0,
                step=1000.0,
                value=service.initial_capital_cents() / 100,
                format="%.2f",
                key=f"initial_{tag}",
            )
            if st.form_submit_button("保存初始本金", key=f"initial_btn_{tag}"):
                try:
                    has_trades = repo.has_trades()
                    service.set_initial_capital(yuan_to_cents(str(new_initial_yuan)))
                    flash(
                        "success",
                        "初始本金已更新：净值基准已同步，历史快照已清空。"
                        if has_trades
                        else "初始本金已更新：期初现金与净值基准已同步，历史快照已清空。",
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

        with st.form(f"cash_form_{tag}"):
            move_yuan = st.number_input(
                "入金/出金金额 (元)",
                min_value=0.01,
                step=100.0,
                value=10000.0,
                format="%.2f",
                key=f"move_{tag}",
            )
            direction = st.radio(
                "方向", ["入金", "出金"], horizontal=True, key=f"dir_{tag}"
            )
            if st.form_submit_button("提交出入金", key=f"cash_btn_{tag}"):
                cents = yuan_to_cents(str(move_yuan))
                try:
                    if direction == "入金":
                        service.deposit(cents)
                    else:
                        service.withdraw(cents)
                    flash("success", f"{direction} ¥{yuan(cents):,.2f} 已记账，现金已更新。")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    tab_overview, tab_trade, tab_action, tab_lots, tab_nav, tab_ingest = st.tabs(
        ["总览", "交易录入", "公司行为", "持仓批次", "净值曲线", "数据导入"]
    )

    # ---------------- 总览 ----------------
    with tab_overview:
        st.subheader("当前持仓汇总")
        summary = service.holdings_summary()
        if summary:
            st.dataframe(pd.DataFrame(summary), hide_index=True, width="stretch")
            if any(row["最新价(元)"] is None for row in summary):
                st.caption(
                    "无最新行情的标的按成本估值，市值与浮盈留空；可在【数据导入】上传日线行情。"
                )
        else:
            st.info("当前无持仓。")

        if st.button("按今日行情结算净值", key=f"settle_{tag}"):
            snapshot = settle_day(repo, pf, date.today())
            st.success(
                f"已结算：总资产 ¥{yuan(snapshot.total_asset_cents):,.2f}，"
                f"当日 {snapshot.day_return_bp / 100:.2f}%"
            )

        rows = repo.dividends_pending()
        if rows:
            st.subheader("待缴红利税（卖出时按批次持股期限补扣）")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "代码": r["symbol"],
                            "批次": r["lot_id"],
                            "买入日": r["open_date"],
                            "除息日": r["ex_date"],
                            "待缴基数(元)": yuan(r["remaining_gross_cents"]),
                        }
                        for r in rows
                    ]
                ),
                hide_index=True,
                width="stretch",
            )

    # ---------------- 交易 ----------------
    with tab_trade:
        with st.form(f"trade_form_{tag}"):
            col1, col2, col3, col4 = st.columns(4)
            symbol = col1.text_input("代码", "510300", key=f"trade_symbol_{tag}")
            trade_date = col2.date_input("成交日期", date.today(), key=f"trade_date_{tag}")
            qty = col3.number_input(
                "数量(股)", min_value=100, step=100, value=1000, key=f"trade_qty_{tag}"
            )
            price = col4.number_input(
                "价格(元)",
                min_value=0.001,
                step=0.001,
                value=4.000,
                format="%.3f",
                key=f"trade_price_{tag}",
            )
            side = st.radio(
                "方向", ["买入", "卖出"], horizontal=True, key=f"trade_side_{tag}"
            )
            submitted = st.form_submit_button("提交")

        if submitted:
            try:
                if side == "买入":
                    trade = service.buy(symbol, trade_date, int(qty), str(price))
                else:
                    trade = service.sell(symbol, trade_date, int(qty), str(price))
                flash(
                    "success",
                    f"{side} {symbol} {trade.qty} 股 @ {trade.price_mills / 1000:.3f}，"
                    f"佣金 ¥{yuan(trade.commission_cents):.2f}，"
                    f"印花税 ¥{yuan(trade.stamp_tax_cents):.2f}，"
                    f"过户费 ¥{yuan(trade.transfer_fee_cents):.2f}，"
                    f"红利税补扣 ¥{yuan(trade.dividend_tax_cents):.2f}",
                )
                st.rerun()
            except Exception as exc:  # 边界校验信息直接展示给用户
                st.error(str(exc))

        st.subheader("交易流水")
        st.caption(
            "已实现盈亏按移动加权平均成本法：现金分红抵扣成本，卖出费用全额扣除。"
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "日期": r["trade_date"],
                        "代码": r["symbol"],
                        "方向": "买入" if r["side"] == "BUY" else "卖出",
                        "数量": r["qty"],
                        "价格": r["price_mills"] / 1000,
                        "佣金": yuan(r["commission_cents"]),
                        "印花税": yuan(r["stamp_tax_cents"]),
                        "过户费": yuan(r["transfer_fee_cents"]),
                        "红利税": yuan(r["dividend_tax_cents"]),
                        "已实现盈亏": yuan(r["realized_pnl_cents"]),
                    }
                    for r in repo.list_trades()
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    # ---------------- 公司行为 ----------------
    with tab_action:
        st.write("除权参考价 = (前收盘价 - 每股现金红利) ÷ (1 + 每股送转比例)")
        with st.form(f"action_form_{tag}"):
            col1, col2, col3 = st.columns(3)
            a_symbol = col1.text_input("代码 ", "510300", key=f"action_symbol_{tag}")
            a_date = col2.date_input(
                "除权除息日", date.today(), key=f"action_date_{tag}"
            )
            a_prev = col3.number_input(
                "前收盘价(元)",
                min_value=0.001,
                value=4.000,
                format="%.3f",
                key=f"action_prev_{tag}",
            )
            col4, col5, col6 = st.columns(3)
            a_cash = col4.number_input(
                "每股现金红利(元)",
                min_value=0.0,
                value=0.0,
                format="%.3f",
                key=f"action_cash_{tag}",
            )
            a_bonus = col5.number_input(
                "每股送股(股)",
                min_value=0.0,
                value=0.0,
                format="%.3f",
                key=f"action_bonus_{tag}",
            )
            a_transfer = col6.number_input(
                "每股转增(股)",
                min_value=0.0,
                value=0.0,
                format="%.3f",
                key=f"action_transfer_{tag}",
            )
            a_submit = st.form_submit_button("执行除权除息")

        if a_submit:
            from core.corporate_actions import CorporateAction

            try:
                action = CorporateAction(
                    symbol=a_symbol,
                    ex_date=a_date,
                    cash_per_share_mills=yuan_to_mills(Decimal(str(a_cash))),
                    bonus_ratio=Decimal(str(a_bonus)),
                    transfer_ratio=Decimal(str(a_transfer)),
                )
                result = service.apply_action(action, str(a_prev))
                flash(
                    "success",
                    "除权除息事件已成功执行！现金和股数已更新。"
                    f"（除权价 {result.ex_price_mills / 1000:.3f}，"
                    f"派现 ¥{yuan(result.dividend_gross_cents):,.2f}，"
                    f"送转 +{result.qty_added} 股）",
                )
                st.rerun()
            except Exception as exc:
                flash("error", f"除权除息执行失败：{exc}")
                st.rerun()

    # ---------------- 批次 ----------------
    with tab_lots:
        st.caption(
            "每笔买入独立批次；卖出按买入日 FIFO 消耗，红利税按批次持股期限判定。"
            "「原始成本」含买入费用且锁定不变，「批次摊薄成本」扣除本批次累计现金分红（送转后自动摊薄）；"
            "账户级券商摊薄成本（含已实现盈亏）见「持仓汇总」。"
        )
        st.dataframe(pd.DataFrame(service.positions()), hide_index=True, width="stretch")

        details = service.lot_corporate_actions()
        with st.expander("批次关联的公司行为明细", expanded=False):
            if details:
                st.dataframe(pd.DataFrame(details), hide_index=True, width="stretch")
            else:
                st.caption("暂无已落账的分红送转事件。")

    # ---------------- 净值 ----------------
    with tab_nav:
        snapshots = repo.list_snapshots()
        if len(snapshots) < 2:
            st.info("净值快照不足，先在总览页按日结算几次。")
        else:
            frame = pd.DataFrame(
                {
                    "日期": [s.snap_date for s in snapshots],
                    "净值": [s.nav_scaled / 1_000_000 for s in snapshots],
                    "总资产": [yuan(s.total_asset_cents) for s in snapshots],
                }
            ).set_index("日期")
            st.line_chart(frame)
            from core.valuation import annualized_return_bp, max_drawdown_bp

            col1, col2 = st.columns(2)
            col1.metric("年化收益率", f"{annualized_return_bp(snapshots) / 100:.2f}%")
            col2.metric("最大回撤", f"{max_drawdown_bp(snapshots) / 100:.2f}%")

    # ---------------- 导入 ----------------
    with tab_ingest:
        st.subheader("在线下载历史行情")
        st.caption(
            "数据源：东方财富（主）/ 腾讯行情（兜底）；日线收盘价、不复权。"
            "行情为全账户共享，下载后总览市值与净值会立即使用最新收盘价。"
        )

        report_key = f"_dl_report_{tag}"
        if report_key in st.session_state:
            report = st.session_state[report_key]
            del st.session_state[report_key]
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "代码": item.symbol,
                            "写入条数": item.rows,
                            "起始日期": item.first_date.isoformat() if item.first_date else "",
                            "结束日期": item.last_date.isoformat() if item.last_date else "",
                            "错误": item.error or "",
                        }
                        for item in report
                    ]
                ),
                hide_index=True,
                width="stretch",
            )

        with st.form(f"download_form_{tag}"):
            raw_symbols = st.text_area(
                "股票 / ETF 代码（逗号或换行分隔）",
                "510300, 600000",
                height=80,
                key=f"dl_symbols_{tag}",
            )
            col_a, col_b = st.columns(2)
            dl_start = col_a.date_input(
                "开始日期",
                date.today() - timedelta(days=365),
                key=f"dl_start_{tag}",
            )
            dl_end = col_b.date_input("结束日期", date.today(), key=f"dl_end_{tag}")
            download_submit = st.form_submit_button("下载并入库")

        if download_submit:
            targets = split_symbols(raw_symbols)
            if not targets:
                st.warning("请输入至少一个股票或 ETF 代码")
            elif dl_start > dl_end:
                st.error("开始日期不能晚于结束日期")
            else:
                try:
                    results = download_history(repo, targets, dl_start, dl_end)
                except Exception as exc:
                    st.error(f"下载失败：{exc}")
                else:
                    st.session_state[report_key] = results
                    ok = [r for r in results if not r.error]
                    failed = [r for r in results if r.error]
                    if failed:
                        detail = "；".join(f"{r.symbol}: {r.error}" for r in failed[:2])
                        flash(
                            "warning",
                            f"成功 {len(ok)} 个、失败 {len(failed)} 个。{detail}",
                        )
                    else:
                        flash(
                            "success",
                            f"已下载 {len(ok)} 个标的，共写入 "
                            f"{sum(r.rows for r in ok)} 条收盘价。",
                        )
                    st.rerun()

        st.divider()
        st.subheader("单日行情查询（不复权）")
        st.caption(
            "调用 services.quotes_service.get_daily_ohlc：返回前收盘 / 开 / 高 / 低 / 收，"
            "非交易日可自动回退到最近交易日并标记实际日期。"
        )
        with st.form(f"ohlc_form_{tag}"):
            ohlc_col1, ohlc_col2 = st.columns(2)
            ohlc_symbol = ohlc_col1.text_input("代码", "600000", key=f"ohlc_symbol_{tag}")
            ohlc_date = ohlc_col2.date_input("日期", date.today(), key=f"ohlc_date_{tag}")
            ohlc_allow_prev = st.checkbox(
                "无当日行情时回退到最近交易日", value=True, key=f"ohlc_prev_{tag}"
            )
            ohlc_submit = st.form_submit_button("查询行情")

        if ohlc_submit:
            quote = get_daily_ohlc(
                ohlc_symbol, ohlc_date, allow_previous_trading_day=ohlc_allow_prev
            )
            if not quote.ok:
                st.error(f"查询失败：{quote.error}")
            else:
                if not quote.is_exact_date:
                    st.warning(
                        f"{ohlc_symbol} 在 {ohlc_date} 无交易，已回退到 "
                        f"{quote.price_date} 的行情。"
                    )
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "代码": quote.ticker,
                                "请求日期": quote.requested_date.isoformat(),
                                "实际交易日": quote.price_date.isoformat(),
                                "前收盘": f"{quote.prev_close_mills / 1000:.3f}"
                                if quote.prev_close_mills is not None
                                else "",
                                "开盘": f"{quote.open_mills / 1000:.3f}",
                                "最高": f"{quote.high_mills / 1000:.3f}",
                                "最低": f"{quote.low_mills / 1000:.3f}",
                                "收盘": f"{quote.close_mills / 1000:.3f}",
                            }
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )

        st.divider()
        st.subheader("公司行为自动同步（分红送转）")
        st.caption(
            "调用 services.corporate_actions_service.fetch_and_sync_dividends："
            "按除权日只让当时在册的批次参与，红利税仍按 FIFO 在卖出时补扣。"
            "ETF 分红常无第三方数据，请在【公司行为】页手动录入。"
        )
        sync_report_key = f"_ca_report_{tag}"
        if sync_report_key in st.session_state:
            st.dataframe(
                pd.DataFrame(st.session_state.pop(sync_report_key)),
                hide_index=True,
                width="stretch",
            )
        with st.form(f"div_sync_form_{tag}"):
            sync_symbol = st.text_input("代码", "600000", key=f"div_symbol_{tag}")
            sync_submit = st.form_submit_button("下载并落账")

        if sync_submit:
            sync_result = CorporateActionService(conn, account_id).fetch_and_sync_dividends(
                sync_symbol
            )
            detail = [
                {
                    "代码": sync_result.ticker,
                    "下载条数": sync_result.fetched,
                    "已落账": sync_result.applied,
                    "已跳过": sync_result.skipped,
                    "错误": "；".join(sync_result.errors) or sync_result.note,
                }
            ]
            st.session_state[sync_report_key] = detail + sync_result.applied_events
            if sync_result.errors:
                flash("warning", "同步完成但有失败：" + "；".join(sync_result.errors))
            elif sync_result.applied:
                flash("success", f"已落账 {sync_result.applied} 条公司行为事件。")
            else:
                flash(
                    "info",
                    sync_result.note
                    or f"无新增事件（跳过 {sync_result.skipped} 条）。",
                )
            st.rerun()

        st.divider()
        quotes_file = st.file_uploader(
            "日线行情 CSV（date,symbol,close）", type="csv", key=f"quotes_file_{tag}"
        )
        if quotes_file and st.button("导入行情", key=f"quotes_btn_{tag}"):
            with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as tmp:
                tmp.write(quotes_file.getbuffer())
                path = tmp.name
            st.success(f"已导入 {import_quotes_csv(repo, path)} 行行情")

        actions_file = st.file_uploader(
            "分红送转 CSV（symbol,ex_date,cash_per_share,bonus_ratio,transfer_ratio）",
            type="csv",
            key=f"actions_file_{tag}",
        )
        if actions_file and st.button("导入并执行公司行为", key=f"actions_btn_{tag}"):
            with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as tmp:
                tmp.write(actions_file.getbuffer())
                path = tmp.name
            count = 0
            for action in load_actions_csv(path):
                prev = repo.latest_price(action.symbol, action.ex_date - timedelta(days=1))
                if prev is None:
                    st.warning(f"{action.symbol} 缺少 {action.ex_date} 前收盘价，已跳过")
                    continue
                service.apply_action(action, f"{prev / 1000:.3f}")
                count += 1
            st.success(f"已执行 {count} 条公司行为")


st.title("长衡")
st.caption("自定义交易规则 · 长期投资账本 · 多账户")
show_flash()

accounts = account_service.list_accounts_with_summary()
if not accounts:
    st.warning("当前没有可用账户，请在侧边栏新建。")
else:
    account_tabs = st.tabs([row["name"] for row in accounts])
    for account_tab, row in zip(account_tabs, accounts):
        with account_tab:
            render_account(int(row["account_id"]))
