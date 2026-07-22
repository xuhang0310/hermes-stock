from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.core import models  # noqa: F401
from backend.app.core.database import Base
from backend.app.core.models import (
    AgentProfile,
    AgentReport,
    AgentToolApproval,
    AiChatSession,
    DailyBar,
    Event,
    Instrument,
    JobRun,
    Position,
    PositionAccount,
    PositionHoldingSnapshot,
    RiskCheck,
    ScheduledJob,
    User,
    WatchlistGroup,
    WatchlistItem,
)
from backend.app.data.repository import StockRepository


def test_dashboard_uses_real_rows_and_remains_read_only() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.utcnow().replace(microsecond=0)

    with Session(engine) as db:
        user = User(
            username="dashboard-user",
            display_name="Dashboard User",
            role="admin",
            permission_scope="all",
            password_hash="unused",
        )
        account = PositionAccount(
            broker_name="测试券商",
            account_name="主账户",
            account_type="cash",
            market="A-share",
            currency="CNY",
            status="active",
            total_asset=328_600,
            available_cash=78_600,
            holding_market_value=250_000,
            total_pnl=18_600,
            day_pnl=4_286,
            fund_updated_at=now,
        )
        db.add_all([user, account])
        db.flush()

        instruments = [
            Instrument(symbol="600519.SH", name="贵州茅台", market="SH", type="stock"),
            Instrument(symbol="000001.SH", name="上证指数", market="SH", type="index"),
            Instrument(symbol="399001.SZ", name="深证成指", market="SZ", type="index"),
            Instrument(symbol="399006.SZ", name="创业板指", market="SZ", type="index"),
        ]
        group = WatchlistGroup(name="默认自选", max_items=30, status="active")
        agent = AgentProfile(agent_name="researcher", display_name="个股研究员", enabled=True)
        db.add_all([*instruments, group, agent])
        db.flush()

        position = Position(
            account_id=account.id,
            symbol="600519.SH",
            position_pct=25.3,
            cost_price=1_500,
            quantity=100,
            stop_price=1_550,
            status="open",
            opened_at=now - timedelta(days=20),
        )
        db.add(position)
        db.flush()
        db.add_all(
            [
                PositionHoldingSnapshot(
                    account_id=account.id,
                    symbol="600519.SH",
                    symbol_name="贵州茅台",
                    as_of_date=now.date().isoformat(),
                    position_quantity=100,
                    cost_price=1_500,
                    latest_price=1_588,
                    market_value=158_800,
                    floating_pnl=8_800,
                    floating_pnl_pct=5.87,
                    day_pnl=2_000,
                    position_pct=25.3,
                    stop_price=1_550,
                    status="open",
                    captured_at=now,
                ),
                WatchlistItem(symbol="600519.SH", group_id=group.id, status="active"),
                AgentReport(
                    report_id="report-dashboard-1",
                    agent_name="researcher",
                    symbol="600519.SH",
                    title="贵州茅台跟踪报告",
                    conclusion="保持观察，等待量价确认。",
                    action_suggestion="observe",
                    content="dashboard contract",
                    created_at=now,
                ),
                RiskCheck(
                    symbol="600519.SH",
                    position_id=position.id,
                    risk_level="high",
                    risk_type="stop_loss",
                    finding="价格接近止损线",
                    required_action="复核仓位",
                    created_at=now,
                ),
                Event(
                    event_id="dashboard-event-1",
                    level="L0",
                    event_type="risk",
                    symbol="600519.SH",
                    title="高风险提醒",
                    summary="测试事件",
                    source_type="test",
                    created_at=now,
                ),
            ]
        )
        for symbol, close, change_pct in (
            ("000001.SH", 3562.74, 0.56),
            ("399001.SZ", 10842.31, 0.18),
            ("399006.SZ", 2231.57, -0.27),
        ):
            db.add(
                DailyBar(
                    symbol=symbol,
                    trade_date=now.date().isoformat(),
                    close=close,
                    change_pct=change_pct,
                    source="test-cache",
                    created_at=now,
                )
            )

        job = ScheduledJob(
            job_key="close-review",
            name="收盘快评",
            schedule_type="daily",
            run_time="15:30",
            weekday_mask="1,2,3,4,5",
            trading_day_only=True,
            digital_human_code="researcher",
            enabled=True,
            next_run_at=now + timedelta(hours=1),
        )
        session = AiChatSession(user_id=user.id, title="生成研究报告", status="active")
        db.add_all([job, session])
        db.flush()
        db.add_all(
            [
                JobRun(
                    run_id="run-dashboard-1",
                    job_key=job.job_key,
                    fire_key="dashboard-fire-1",
                    trigger_type="schedule",
                    status="running",
                    started_at=now - timedelta(minutes=2),
                ),
                AgentToolApproval(
                    approval_id="approval-dashboard-1",
                    session_id=session.id,
                    user_id=user.id,
                    tool_name="ws_run_python",
                    display_name="执行工作区脚本",
                    risk_level="high",
                    arguments_json='{"script_path":"scripts/report.py"}',
                    status="pending",
                    expires_at=now + timedelta(minutes=5),
                    created_at=now,
                ),
            ]
        )
        db.commit()

        payload = StockRepository(db).dashboard(user_id=user.id)

        assert payload["portfolio"]["total_asset"] == 328_600
        assert payload["counts"]["open_positions"] == 1
        assert payload["counts"]["watchlist"] == 1
        assert payload["counts"]["high_risk_24h"] == 1
        assert payload["counts"]["pending_approvals"] == 1
        assert payload["counts"]["running_jobs"] == 1
        assert len(payload["market"]["items"]) == 3
        assert payload["market"]["items"][0]["price"] == 3562.74
        assert payload["positions"][0]["latest_research"]["report_id"] == "report-dashboard-1"
        assert payload["positions"][0]["risk_state"]["level"] == "high"
        assert payload["runtime"]["running"][0]["run_id"] == "run-dashboard-1"
        assert payload["approvals"][0]["approval_id"] == "approval-dashboard-1"
        assert payload["approvals"][0]["expires_at"].endswith("+00:00")
        assert payload["next_job"]["job_key"] == "close-review"
        assert not db.new
        assert not db.dirty
        assert not db.deleted

    engine.dispose()
