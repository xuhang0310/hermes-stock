"""Isolated contract tests: no external feeds or application database writes."""
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth.auth import AuthContext, create_access_token, get_current_user
from backend.app.core import database
from backend.app.core.models import Instrument, DailyBar, StockDailyBasic, StockLatestPrice, MarketTradingCalendar, User
from backend.app.market import industry_service as service
from backend.app.market.industry_api import router
from backend.app.quant import screener_service, screener_ai
from backend.app.quant.screener_api import router as screener_router, ScreenerQueryIn

NOW = datetime(2026, 9, 19, 17, tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    tables = [Instrument, DailyBar, StockDailyBasic, StockLatestPrice, MarketTradingCalendar, User]
    database.Base.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(service, 'SessionLocal', factory)
    monkeypatch.setattr(screener_service, 'SessionLocal', factory)
    monkeypatch.setattr(database, 'SessionLocal', factory)
    with factory() as db:
        for index in range(10):
            symbol = f'60000{index}.SH'
            db.add(Instrument(symbol=symbol, name=f'样本{index}', market='SH', type='stock',
                              industry=' 银行 ' if index < 5 else '软件服务', list_status='L', list_date='20000101'))
            db.add(StockLatestPrice(symbol=symbol, name=f'样本{index}', price=10, pct_chg=index, trade_date='20260918', source='tushare', fetched_at=NOW))
        db.add_all([MarketTradingCalendar(exchange='SSE', cal_date='20260918', is_open=True, source='tushare'),
                    MarketTradingCalendar(exchange='SSE', cal_date='20260919', is_open=False, source='tushare')])
        db.commit()
    yield factory
    engine.dispose()


def bar(index, day='20260918', **kwargs):
    values = dict(symbol=f'60000{index}.SH', trade_date=day, close=10, pre_close=10,
                  change_pct=float(index - 2), volume=100, amount=100_000, source='tushare')
    values.update(kwargs)
    return DailyBar(**values)


def seed(factory, rows):
    with factory() as db:
        db.add_all(rows)
        db.commit()


def test_uses_one_day_falls_back_from_partial_and_deduplicates(db_factory):
    seed(db_factory, [bar(i, '20260917') for i in range(10)] + [bar(0), bar(0, '2026-09-17', change_pct=9)])
    snapshot = service.load_snapshot(now=NOW)
    overview = snapshot['overview']
    assert overview['as_of'] == '2026-09-17'
    assert overview['latest_available_date'] == '2026-09-18'
    assert overview['coverage']['bar_count'] == 10
    assert overview['coverage_status'] == 'sufficient'
    assert all(member['trade_date'] == '2026-09-17' for member in snapshot['members'])
    assert snapshot['members'][0]['pct_chg'] == 9
    assert any('覆盖不足' in warning for warning in overview['warnings'])
    assert any('最近已收盘' in warning for warning in overview['warnings'])


def test_missing_and_zero_volume_are_not_flat_and_units_are_correct(db_factory):
    seed(db_factory, [bar(i) for i in range(3)] + [bar(3, volume=0), bar(4, close=-1)] +
         [StockDailyBasic(symbol='600000.SH', trade_date='20260918', total_mv=500_000, turnover_rate=3, volume_ratio=1.5),
          StockDailyBasic(symbol='600001.SH', trade_date='20260917', total_mv=999_000)])
    snapshot = service.load_snapshot(now=NOW)
    stats = next(item for item in snapshot['overview']['industries'] if item['industry'] == '银行')
    assert stats['avg_pct'] == -1
    assert stats['median_pct'] == -1
    assert (stats['up_count'], stats['down_count'], stats['flat_count']) == (0, 2, 1)
    assert (stats['valid_count'], stats['no_trade_count'], stats['invalid_count']) == (3, 1, 1)
    assert stats['amount_yi'] == 3
    assert snapshot['members'][0]['total_mv_yi'] == 50
    assert snapshot['members'][1]['total_mv_yi'] is None  # never carry older basics forward
    assert snapshot['members'][3]['pct_chg'] is None
    assert snapshot['overview']['coverage']['missing_count'] == 5
    assert snapshot['overview']['coverage']['daily_basic_count'] == 1
    assert any('同日每日指标' in warning for warning in snapshot['overview']['warnings'])
    assert snapshot['overview']['coverage_status'] == 'partial'


def test_ignores_other_sources_funds_delisted_and_future_listings(db_factory):
    seed(db_factory, [bar(0, source='other', amount=100_000_000),
         Instrument(symbol='900001.SH', name='沪B股', market='SH', type='stock', list_status='L', industry='银行'),
         Instrument(symbol='200001.SZ', name='深B股', market='SZ', type='stock', list_status='L', industry='银行'),
         Instrument(symbol='600010.SH', name='退市', market='SH', type='stock', list_status='D', industry='银行'),
         Instrument(symbol='510000.SH', name='基金', market='SH', type='fund', industry='银行'),
         Instrument(symbol='600011.SH', name='待上市', market='SH', type='stock', list_status='L', list_date='20260921', industry='银行')])
    result = service.load_snapshot(now=NOW)['overview']
    assert result['as_of'] is None
    assert result['coverage']['stock_count'] == 10
    assert result['coverage_status'] == 'empty'


def test_unknown_classification_is_disclosed_and_not_in_rankings(db_factory):
    seed(db_factory, [bar(i) for i in range(10)])
    with db_factory() as db:
        db.get(Instrument, '600000.SH').industry = None
        db.commit()
    result = service.load_snapshot(now=NOW)['overview']
    assert result['coverage']['unclassified_count'] == 1
    assert sum(item['stock_count'] for item in result['industries']) == 9
    assert any('缺少行业分类' in warning for warning in result['warnings'])


def test_snapshot_fingerprint_tracks_same_date_corrections(db_factory):
    seed(db_factory, [bar(i) for i in range(10)])
    before = service.load_snapshot(now=NOW)['overview']['snapshot_id']
    assert before == service.load_snapshot(now=NOW)['overview']['snapshot_id']
    with db_factory() as db:
        db.query(DailyBar).filter(DailyBar.symbol == '600000.SH').update({'change_pct': 6})
        db.commit()
    assert before != service.load_snapshot(now=NOW)['overview']['snapshot_id']


def test_members_sort_null_last_in_both_directions_and_paginate(db_factory):
    seed(db_factory, [bar(0), bar(1), bar(2)])
    snapshot = service.load_snapshot(now=NOW)
    asc = service.industry_members(snapshot, '银行', order='asc')
    desc = service.industry_members(snapshot, '银行', order='desc')
    assert [item['pct_chg'] for item in asc['items']] == [-2, -1, 0, None, None]
    assert [item['pct_chg'] for item in desc['items']] == [0, -1, -2, None, None]
    page = service.industry_members(snapshot, '银行', offset=2, limit=2)
    assert page['total'] == 5 and len(page['items']) == 2
    assert service.industry_members(snapshot, '不存在')['items'] == []


def test_before_close_excludes_today_and_calendar_gap_is_visible(db_factory):
    seed(db_factory, [bar(i, '20260919') for i in range(10)] + [bar(i) for i in range(10)])
    before_close = NOW.replace(hour=14)
    assert service.load_snapshot(now=before_close)['overview']['as_of'] == '2026-09-18'
    with db_factory() as db:
        db.execute(delete(MarketTradingCalendar))
        db.commit()
    result = service.load_snapshot(now=before_close)['overview']
    assert result['expected_date'] is None
    assert any('日历不足' in warning for warning in result['warnings'])


def test_nonfinite_metrics_are_not_serialized_as_numbers(db_factory):
    seed(db_factory, [bar(0, change_pct=float('inf'), pre_close=0, amount=float('inf'))])
    result = service.load_snapshot(now=NOW)
    member = result['members'][0]
    assert member['status'] == 'invalid'
    assert member['pct_chg'] is None and member['amount_yi'] is None


@pytest.fixture
def client(db_factory, monkeypatch):
    actual_load = service.load_snapshot
    monkeypatch.setattr(service, 'load_snapshot', lambda: actual_load(now=NOW))
    app = FastAPI()
    app.include_router(router)
    app.include_router(screener_router)
    with TestClient(app) as test_client:
        yield test_client


def authenticate(client, factory):
    with factory() as db:
        user = User(username='industry-viewer', display_name='只读', role='viewer', password_hash='test')
        db.add(user)
        db.commit()
        client.headers['Authorization'] = f'Bearer {create_access_token(user)}'


def test_api_requires_login_and_data_read_permission(client, db_factory):
    assert client.get('/api/industries/overview').status_code == 401
    authenticate(client, db_factory)
    assert client.get('/api/industries/overview').status_code == 200
    client.app.dependency_overrides[get_current_user] = lambda: AuthContext(user=User(id=100), permissions=set())
    assert client.get('/api/industries/overview').status_code == 403


def test_api_validates_snapshot_pagination_sort_and_detects_update(client, db_factory):
    seed(db_factory, [bar(i) for i in range(10)])
    authenticate(client, db_factory)
    overview = client.get('/api/industries/overview').json()['data']
    params = {'industry': '银行', 'snapshot_id': overview['snapshot_id']}
    response = client.get('/api/industries/members', params=params)
    assert response.status_code == 200 and response.json()['data']['total'] == 5
    for invalid in [{'sort_by': 'name'}, {'limit': 101}, {'offset': -1}, {'snapshot_id': 'bad'}]:
        assert client.get('/api/industries/members', params={**params, **invalid}).status_code == 422
    assert client.get('/api/industries/members', params={'industry': '银行'}).status_code == 422
    seed(db_factory, [StockDailyBasic(symbol='600000.SH', trade_date='20260918', turnover_rate=2)])
    assert client.get('/api/industries/members', params=params).status_code == 409


def test_industry_screener_filter_is_exact_and_preserved(client, db_factory):
    authenticate(client, db_factory)
    query = ScreenerQueryIn(industry='银行', pct_chg_min=2).model_dump()
    response = client.post('/api/screener/run', json=query)
    assert response.status_code == 200
    items = response.json()['data']['items']
    assert {item['symbol'] for item in items} == {'600002.SH', '600003.SH', '600004.SH'}
    assert screener_service.count_screener_candidates(query) == 3
    assert screener_service.run_screener(industry='银')['total'] == 0
    assert screener_ai._sanitize({'industry': ' 银行 '})['industry'] == '银行'
    assert '行业「银行」' in screener_ai._summarize(query)
