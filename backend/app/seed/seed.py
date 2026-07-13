from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from backend.app.auth.auth import hash_password
from backend.app.data.broker_presets import DEFAULT_BROKER_CODES, broker_name_for
from backend.app.core.models import (
    AgentProfile,
    AgentReport,
    AgentTask,
    AiChatSession,
    DigitalHumanProfile,
    Event,
    EventEvidence,
    Instrument,
    LlmModelConfig,
    LlmModelPlatform,
    NotifyChannel,
    Position,
    PositionAccount,
    RiskCheck,
    SystemLog,
    TradeReport,
    User,
    WatchlistGroup,
    WatchlistItem,
)
from backend.app.core.settings import settings


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def seed_demo(db: Session) -> None:
    timestamp = now_utc()
    seed_auth_and_ai_configs(db)
    seed_notify_channels(db)
    seed_agents(db)
    seed_instruments(db)
    seed_watchlist_and_position(db, timestamp)
    seed_event_flow(db, timestamp)
    db.add(
        SystemLog(
            level="INFO",
            module="seed",
            message="demo A-share dataset inserted",
            context_json=json.dumps({"scope": "cn_a_share"}, ensure_ascii=False),
        )
    )
    db.commit()


def _upsert(db: Session, model: type, lookup: dict, defaults: dict):
    instance = db.scalar(select(model).filter_by(**lookup).limit(1))
    payload = {**lookup, **defaults}
    if instance is None:
        instance = model(**payload)
        db.add(instance)
        db.flush()
        return instance
    for key, value in defaults.items():
        if key == "api_key" and value is None and getattr(instance, key, None):
            continue
        if key == "password_hash" and getattr(instance, key, None):
            continue
        setattr(instance, key, value)
    db.flush()
    return instance


def seed_notify_channels(db: Session) -> None:
    """预置飞书告警群/日报群两条内置通道。只补缺行，绝不覆盖用户已改配置。"""
    builtin_rows = [
        {
            "channel_key": "feishu-alert",
            "name": "飞书告警群",
            "purpose": "alert",
            "remark": "零噪音纪律：只发 L0 警报、心跳异常和系统故障，不发日报。",
        },
        {
            "channel_key": "feishu-daily",
            "name": "飞书日报群",
            "purpose": "daily",
            "remark": "盘前简报、开盘快报、收盘快评和晚间日报的推送通道，可堆积可回溯。",
        },
    ]
    for row in builtin_rows:
        existing = db.scalar(select(NotifyChannel).where(NotifyChannel.channel_key == row["channel_key"]).limit(1))
        if existing is None:
            db.add(NotifyChannel(**row, channel_type="feishu_webhook", status="disabled"))
    db.flush()


def seed_position_accounts(db: Session) -> None:
    """预置当前在用的券商账户（方正/广发/中信）。按 broker_code 幂等，不覆盖已有资金。"""
    for index, code in enumerate(DEFAULT_BROKER_CODES):
        existing = db.scalar(select(PositionAccount).where(PositionAccount.broker_code == code).limit(1))
        if existing is None:
            db.add(
                PositionAccount(
                    broker_code=code,
                    broker_name=broker_name_for(code) or code,
                    account_type="cash",
                    market="A-share",
                    currency="CNY",
                    status="active",
                    is_default=(index == 0),
                    sort_order=index * 10,
                )
            )
    db.flush()


def seed_auth_and_ai_configs(db: Session) -> None:
    # 仅当库里没有任何启用用户、且显式配置了管理员凭据时才创建初始账号；
    # 不配置就不写入任何默认账号（避免硬编码 admin/admin123456 这类默认凭据）。
    has_enabled_user = db.scalar(select(User).where(User.is_enabled.is_(True)).limit(1))
    if has_enabled_user is None and settings.admin_username and settings.admin_password:
        _upsert(
            db,
            User,
            {"username": settings.admin_username},
            {
                "display_name": "系统管理员",
                "role": "super_admin",
                "permission_scope": "all",
                "password_hash": hash_password(settings.admin_password),
                "is_enabled": True,
            },
        )

    aggregate_platform_ids = [
        item.id
        for item in db.scalars(select(LlmModelPlatform).where(LlmModelPlatform.name.in_(["聚合AI", "聚合 AI"]))).all()
    ]
    if aggregate_platform_ids:
        aggregate_model_ids = [
            item.id
            for item in db.scalars(
                select(LlmModelConfig).where(
                    (LlmModelConfig.platform_id.in_(aggregate_platform_ids))
                    | (LlmModelConfig.provider_name.in_(["聚合AI", "聚合 AI"]))
                    | (LlmModelConfig.display_name.like("聚合AI%"))
                )
            ).all()
        ]
        if aggregate_model_ids:
            db.execute(update(AiChatSession).where(AiChatSession.model_config_id.in_(aggregate_model_ids)).values(model_config_id=None))
            db.execute(delete(LlmModelConfig).where(LlmModelConfig.id.in_(aggregate_model_ids)))
        db.execute(delete(LlmModelPlatform).where(LlmModelPlatform.id.in_(aggregate_platform_ids)))
        db.flush()

    bailian_platform = db.scalar(
        select(LlmModelPlatform).where(LlmModelPlatform.name.in_(["百炼", "阿里云百炼"])).limit(1)
    )
    bailian_base_url = settings.llm_base_url or "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    if bailian_platform is None:
        bailian_platform = LlmModelPlatform(name="百炼")
        db.add(bailian_platform)
    bailian_platform.name = "百炼"
    bailian_platform.provider_type = "DashScope OpenAI compatible"
    bailian_platform.base_url = bailian_base_url
    if settings.llm_api_key:
        bailian_platform.api_key = settings.llm_api_key
    bailian_platform.status = "enabled"
    bailian_platform.remark = "默认百炼 OpenAI 兼容接口，供数字人 ReAct 使用。"
    db.flush()

    legacy_bailian_model_ids = [
        item.id
        for item in db.scalars(
            select(LlmModelConfig).where(
                LlmModelConfig.platform_id == bailian_platform.id,
                LlmModelConfig.model_name.in_(["qwen-plus", "qwen-max", "deepseek-v3"]),
            )
        ).all()
    ]
    if legacy_bailian_model_ids:
        db.execute(
            update(AiChatSession)
            .where(AiChatSession.model_config_id.in_(legacy_bailian_model_ids))
            .values(model_config_id=None)
        )
        db.execute(delete(LlmModelConfig).where(LlmModelConfig.id.in_(legacy_bailian_model_ids)))
        db.flush()

    default_model_name = settings.llm_model or "qwen3.7-plus"
    default_model = _upsert(
        db,
        LlmModelConfig,
        {"display_name": "百炼 Qwen3.7 Plus"},
        {
            "platform_id": bailian_platform.id,
            "provider_name": "百炼",
            "base_url": bailian_platform.base_url,
            "api_key": None,
            "model_name": default_model_name,
            "is_active": True,
            "status": "enabled",
            "temperature": 0.2,
            "max_steps": settings.agent_max_steps,
            "remark": "系统默认百炼 ReAct 模型配置。",
        },
    )
    for display_name, model_name, remark in [
        ("百炼 Qwen3.7 Max", "qwen3.7-max", "复杂研究和长报告模型。"),
        ("百炼 DeepSeek V4 Pro", "deepseek-v4-pro", "推理分析模型。"),
        ("百炼 GLM 5.2", "glm-5.2", "通用研究模型。"),
        ("百炼 Kimi K2.7 Code", "kimi-k2.7-code", "偏代码和结构化分析模型。"),
    ]:
        _upsert(
            db,
            LlmModelConfig,
            {"display_name": display_name},
            {
                "platform_id": bailian_platform.id,
                "provider_name": "百炼",
                "base_url": bailian_platform.base_url,
                "api_key": None,
                "model_name": model_name,
                "is_active": False,
                "status": "enabled",
                "temperature": 0.2,
                "max_steps": 6,
                "remark": remark,
            },
        )
    db.execute(
        update(LlmModelConfig)
        .where(LlmModelConfig.id != default_model.id)
        .values(is_active=False)
    )

    digital_humans = [
        {
            "code": "stock-pm",
            "name": "A股研究总调度",
            "avatar_tone": "冷静、克制、先问事实",
            "description": "适合作为日常入口，负责拆问题、选数据、整理结论和下一步动作。",
            "system_prompt": "你是A股研究总调度。先判断问题属于持仓、事件、自选、研究还是风控，再用最少查询获得证据，最后输出事实、推断、风险和下一步。",
            "quick_prompts": ["今天有哪些需要优先处理的事件？", "帮我生成一份A股研究台日报", "目前系统里最大的风险是什么？", "把自选和持仓的重点列出来"],
            "sort_order": 10,
            "can_write_positions": True,
        },
        {
            "code": "risk-officer",
            "name": "风控复盘官",
            "avatar_tone": "严格、反证、重视止损",
            "description": "围绕仓位、止损、冷静期、反驳理由和复盘动作做检查。",
            "system_prompt": "你是风控复盘官。优先检查仓位、止损、风险事件和Agent报告；对每个建议给出反方观点和可验证条件，不提供自动交易指令。",
            "quick_prompts": ["当前持仓是否触发风控？", "列出需要复盘的止损事件", "帮我反驳一下当前持仓逻辑", "哪些报告建议需要冷静期？"],
            "sort_order": 20,
        },
        {
            "code": "market-sentinel",
            "name": "盘中哨兵",
            "avatar_tone": "短句、敏锐、只报异常",
            "description": "盯盘事件、异动、L0/L1告警，适合快速判断是否需要通知。",
            "system_prompt": "你是盘中哨兵。只关注异常、级别、来源、证据和是否需要通知。输出要短，不做长篇宏观解释。",
            "quick_prompts": ["最近的L0事件是什么？", "哪些事件需要立即通知？", "按标的汇总最近事件", "有没有重复噪音事件？"],
            "sort_order": 30,
        },
        {
            "code": "research-analyst",
            "name": "个股研究员",
            "avatar_tone": "结构化、重证据、少形容词",
            "description": "把事件、报告、持仓理由和证据串成个股研究备忘录。",
            "system_prompt": "你是个股研究员。用系统内证据整理个股备忘录，必须标注样例数据或缺失数据，不得凭空补充外部事实。",
            "quick_prompts": ["给000001.SZ生成研究备忘录", "梳理某个标的的事件和报告", "哪些标的缺少研究证据？", "把报告结论整理成表格"],
            "sort_order": 40,
        },
        {
            "code": "intelligence-officer",
            "name": "情报官",
            "avatar_tone": "谨慎、来源优先、一遍过滤",
            "description": "负责新闻、公告、政策、研报快讯解读，先做可信度过滤，再转交研究或风控。",
            "system_prompt": "你是情报官。必须先区分内部证据、公告新闻、政策行业文本和未经核验的外部搜索结果；输出要标明来源、可信度、影响方向和下一步核验动作。",
            "quick_prompts": ["汇总最近影响持仓和自选池的情报", "哪些外部消息需要核验？", "按可信度排序最近政策/公告/新闻", "把可疑噪音和有效情报分开"],
            "sort_order": 50,
        },
        {
            "code": "quant-analyst",
            "name": "量化分析师",
            "avatar_tone": "数字化、代码感、只认样本",
            "description": "负责因子、筹码、回测、统计套利、盘后维护和异常情形统计表。",
            "system_prompt": "你是量化分析师。优先使用结构化工具输出均线、量能、形态、资金流、筹码和本地K线统计；必须说明样本范围、数据缺口和统计边界，不输出无条件交易指令。",
            "quick_prompts": ["比较持仓和自选池的量化信号", "给000001.SZ生成量化信号快照", "哪些标的出现放量或异常波动？", "列出数据不足无法判断的标的"],
            "sort_order": 60,
        },
        {
            "code": "macro-strategist",
            "name": "宏观策略师",
            "avatar_tone": "框架化、政策敏感、情景推演",
            "description": "负责宏观、政策、利率、外盘、行业轮动和周期预期的背景判断。",
            "system_prompt": "你是宏观策略师。把宏观政策、行业轮动、指数环境和外部风险映射到当前持仓和自选池；必须区分事实、预期和推断，并给出基准、乐观、压力三种情景。",
            "quick_prompts": ["当前宏观背景对持仓有什么影响？", "搜索并解释最近的政策/行业轮动", "给自选池做一版宏观敏感性检查", "列出需要跟踪的宏观验证指标"],
            "sort_order": 70,
        },
        {
            "code": "short-term-hunter",
            "name": "短线猎手",
            "avatar_tone": "快、克制、重纪律",
            "description": "负责涨停、龙头、游资、热榜、情绪周期和短线交易信号观察。",
            "system_prompt": "你是短线猎手。只在持仓和自选池范围内做短线候选扫描，重点看量能、涨跌幅、事件、热点和情绪阶段；必须同时给出仓位上限、止损或冷静期，不输出直接下单。",
            "quick_prompts": ["扫描自选和持仓里的短线候选", "哪些标的出现短线放量信号？", "分析一个热点题材的短线动量", "列出需要冷静观察、不能追高的标的"],
            "sort_order": 80,
        },
    ]
    for item in digital_humans:
        _upsert(
            db,
            DigitalHumanProfile,
            {"code": item["code"]},
            {
                "name": item["name"],
                "avatar_tone": item["avatar_tone"],
                "target_roles": "*",
                "description": item["description"],
                "system_prompt": item["system_prompt"],
                "quick_prompts_json": json.dumps(item["quick_prompts"], ensure_ascii=False),
                "status": "enabled",
                "sort_order": item["sort_order"],
                "can_write_positions": item.get("can_write_positions", False),
            },
        )


def seed_agents(db: Session) -> None:
    rows = [
        ("pm", "总调度 PM", True, "对话入口、分派、统稿、审批"),
        ("sentinel", "盘中哨兵", True, "盘中异常和情绪温度解读"),
        ("intelligence", "情报官", True, "新闻、公告、政策筛选"),
        ("risk", "风控官", True, "仓位、止损、反驳和复盘"),
        ("researcher", "研究员", True, "个股深研和教学式报告"),
        ("quant", "量化分析师", True, "回测、因子和异动统计"),
        ("macro", "宏观策略师", True, "宏观和行业轮动背景"),
        ("hunter", "短线猎手", True, "短线情绪和热点观察"),
    ]
    for agent_name, display_name, enabled, description in rows:
        profile = db.get(AgentProfile, agent_name)
        if profile is None:
            db.add(
                AgentProfile(
                    agent_name=agent_name,
                    display_name=display_name,
                    enabled=enabled,
                    role_description=description,
                )
            )
        else:
            profile.display_name = display_name
            profile.enabled = enabled
            profile.role_description = description


def seed_instruments(db: Session) -> None:
    rows = [
        ("000001.SZ", "平安银行", "SZSE", "stock"),
        ("600519.SH", "贵州茅台", "SSE", "stock"),
        ("000300.SH", "沪深300", "CSI", "index"),
        ("399006.SZ", "创业板指", "CNI", "index"),
    ]
    for symbol, name, market, type_ in rows:
        instrument = db.get(Instrument, symbol)
        if instrument is None:
            db.add(Instrument(symbol=symbol, name=name, market=market, type=type_))
        else:
            instrument.name = name
            instrument.market = market
            instrument.type = type_
            instrument.status = "active"


def seed_watchlist_and_position(db: Session, timestamp: datetime) -> None:
    group = db.scalar(select(WatchlistGroup).where(WatchlistGroup.name == "默认"))
    if group is None:
        group = WatchlistGroup(name="默认", description="第一阶段仅监控持仓、自选池和核心指数")
        db.add(group)
        db.flush()

    for symbol, reason in [
        ("000001.SZ", "银行板块观察样例"),
        ("600519.SH", "消费核心资产观察样例"),
    ]:
        item = db.scalar(
            select(WatchlistItem).where(
                WatchlistItem.symbol == symbol,
                WatchlistItem.group_id == group.id,
            )
        )
        if item is None:
            db.add(WatchlistItem(symbol=symbol, group_id=group.id, reason=reason, status="active"))
        else:
            item.reason = reason
            item.status = "active"
            item.removed_at = None

    position = db.scalar(select(Position).where(Position.symbol == "000001.SZ", Position.status == "open"))
    if position is None:
        db.add(
            Position(
                symbol="000001.SZ",
                position_pct=8.0,
                cost_price=10.20,
                quantity=1000,
                stop_price=9.89,
                thesis="样例持仓：观察银行板块估值修复",
                risk_note="跌破止损需复核",
                status="open",
                opened_at=timestamp,
            )
        )

    if not db.scalar(select(TradeReport).where(TradeReport.symbol == "000001.SZ")):
        db.add(
            TradeReport(
                symbol="000001.SZ",
                action="buy",
                price=10.20,
                position_pct=8.0,
                reason="样例报备：低估值观察仓",
                expectation="等待基本面和板块共振确认",
                stop_condition="收盘跌破9.89复核",
                review_date="T+14",
            )
        )


def seed_event_flow(db: Session, timestamp: datetime) -> None:
    event = db.scalar(select(Event).where(Event.event_id == "demo-l0-stop-loss-000001"))
    facts = {"price": 9.86, "stop_price": 9.89, "change_pct": -3.33, "source": "demo"}
    if event is None:
        event = Event(
            event_id="demo-l0-stop-loss-000001",
            level="L0",
            event_type="stop_loss",
            symbol="000001.SZ",
            title="样例：持仓触及止损线",
            summary="平安银行样例持仓价格低于预设止损线，需要立即复核。",
            facts_json=json.dumps(facts, ensure_ascii=False),
            source_type="rule_engine",
            status="routed",
            created_at=timestamp,
        )
        db.add(event)
        db.flush()
    else:
        event.facts_json = json.dumps(facts, ensure_ascii=False)
        event.status = "routed"

    if not db.scalar(select(EventEvidence).where(EventEvidence.event_id == event.id)):
        db.add(
            EventEvidence(
                event_id=event.id,
                evidence_type="price_snapshot",
                source_table="market_snapshots",
                content="样例证据：价格9.86低于止损9.89",
            )
        )

    task = db.scalar(select(AgentTask).where(AgentTask.task_id == "demo-task-risk-000001"))
    if task is None:
        db.add(
            AgentTask(
                task_id="demo-task-risk-000001",
                event_id=event.event_id,
                agent_name="risk",
                task_type="risk_check",
                input_json=json.dumps({"symbol": "000001.SZ", "event": "stop_loss"}, ensure_ascii=False),
                status="done",
                priority=1,
                created_at=timestamp,
                finished_at=timestamp,
            )
        )

    report = db.scalar(select(AgentReport).where(AgentReport.report_id == "demo-report-risk-000001"))
    if report is None:
        db.add(
            AgentReport(
                report_id="demo-report-risk-000001",
                task_id="demo-task-risk-000001",
                event_id=event.event_id,
                agent_name="risk",
                symbol="000001.SZ",
                title="样例风控意见：触及止损需复核",
                conclusion="不自动下单，但需要立即复核持仓逻辑。",
                reasoning="规则命中来自用户设置止损线和样例价格快照。",
                counter_arguments="若为瞬时异常报价，应等待数据复核。",
                risk_points="继续下跌会扩大亏损；未报备则台账不可信。",
                position_limit="不新增仓位",
                stop_condition="收盘仍低于止损线则进入退出复盘。",
                action_suggestion="review",
                content="样例报告：触及止损线，建议查看依据并加入复盘。",
                model_name="demo-local",
            )
        )

    position = db.scalar(select(Position).where(Position.symbol == "000001.SZ", Position.status == "open"))
    if position and not db.scalar(select(RiskCheck).where(RiskCheck.event_id == event.event_id)):
        db.add(
            RiskCheck(
                symbol="000001.SZ",
                position_id=position.id,
                event_id=event.event_id,
                risk_level="high",
                risk_type="stop_loss",
                finding="样例持仓触及止损线。",
                required_action="查看依据，更新持仓计划或加入复盘。",
                position_limit=8.0,
                stop_condition="收盘仍低于9.89则复盘退出条件。",
                cool_down_required=True,
            )
        )
