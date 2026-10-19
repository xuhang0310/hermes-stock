"""DB/network-free market-cache regression tests.

Load only market_sync.py with small SQLAlchemy model doubles. This also works in
an isolated worktree without the parent's untracked models/scoring modules. No
application startup, conftest, MySQL, or TuShare connection is needed.
"""
from __future__ import annotations

import importlib.util
import sys
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from sqlalchemy import Boolean, Column, Integer, String
from sqlalchemy.orm import declarative_base


Base = declarative_base()


class Instrument(Base):
    __tablename__ = "instruments"
    symbol = Column(String, primary_key=True)
    name = Column(String)
    market = Column(String)
    type = Column(String)
    list_status = Column(String)
    list_date = Column(String)
    industry = Column(String)


class DailyBar(Base):
    __tablename__ = "daily_bars"
    id = Column(Integer, primary_key=True)
    symbol = Column(String)
    trade_date = Column(String)
    source = Column(String)


class StockDailyBasic(Base):
    __tablename__ = "stock_daily_basic"
    id = Column(Integer, primary_key=True)
    symbol = Column(String)
    trade_date = Column(String)


class StockAdjFactor(Base):
    __tablename__ = "stock_adj_factors"
    id = Column(Integer, primary_key=True)
    symbol = Column(String)
    trade_date = Column(String)


class StockLatestPrice(Base):
    __tablename__ = "stock_latest_prices"
    symbol = Column(String, primary_key=True)


class MarketTradingCalendar(Base):
    __tablename__ = "market_trading_calendar"
    exchange = Column(String, primary_key=True)
    cal_date = Column(String, primary_key=True)
    is_open = Column(Boolean)
    source = Column(String)


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 5)  # Saturday: the authoritative row explicitly says closed.


def _module(name, **attrs):
    module = ModuleType(name)
    module.__dict__.update(attrs)
    return module


def _sql(query):
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def _result(rows):
    result = MagicMock()
    result.all.return_value = rows
    return result


class AutomaticSelectionMarketCacheTests(unittest.TestCase):
    def setUp(self):
        # These quality values are test inputs. A separate test varies them to
        # ensure production code imports the shared config instead of copying it.
        self.quality = {"min_bar_coverage": 0.90, "min_basic_coverage": 0.85, "min_history_coverage": 0.85}
        blocked = Mock(side_effect=AssertionError("unexpected real I/O"))
        replacements = {
            "backend.app.core.database": _module("database", SessionLocal=blocked, engine=blocked),
            "backend.app.core.models": _module(
                "models", Instrument=Instrument, DailyBar=DailyBar,
                StockDailyBasic=StockDailyBasic, StockLatestPrice=StockLatestPrice,
                MarketTradingCalendar=MarketTradingCalendar, StockAdjFactor=StockAdjFactor,
            ),
            "backend.app.market.stock_tools": _module(
                "stock_tools", _tushare_request=blocked, get_trade_cal=blocked,
                normalize_a_share_symbol=lambda value: str(value).upper(),
            ),
            "backend.app.quant.automatic_selection_scoring": _module(
                "scoring", DEFAULT_STRATEGY_CONFIG={"quality": self.quality},
            ),
        }
        self.enterContext(patch.dict(sys.modules, replacements))
        path = Path(__file__).resolve().parents[1] / "backend/app/market/market_sync.py"
        spec = importlib.util.spec_from_file_location("_market_sync_under_test", path)
        self.market = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.market)
        self.market.date = FrozenDate
        self.market.time = SimpleNamespace(monotonic=time.monotonic, sleep=Mock())
        self.today = "20260905"
        self.latest = "20260904"
        # Weekdays generate fixture data only; production must use the explicit
        # is_open rows, including closures and holidays, never this approximation.
        start = date(2026, 8, 1)
        self.calendar = [
            {"exchange": "SSE", "cal_date": (start + timedelta(days=i)).strftime("%Y%m%d"),
             "is_open": int((start + timedelta(days=i)).weekday() < 5)}
            for i in range((FrozenDate.today() - start).days + 1)
        ]
        self.history = [row["cal_date"] for row in self.calendar if row["is_open"]][-21:]

    def cache(self, *, calendar_today=True, calendar_date=None, bar_date=None,
              basic_date=None, catalog_count=5000, bar_counts=None, basic_count=4700,
              history=None, calendar_dates=None, adj_counts=None):
        db = MagicMock()
        self.market.SessionLocal = MagicMock()
        self.market.SessionLocal.return_value.__enter__.return_value = db
        db.scalar.side_effect = [
            self.today if calendar_today else None,
            calendar_date or self.latest,
            bar_date or self.latest, None,
            basic_date or self.latest, None,
            catalog_count,
        ]
        expected_history = self.history if history is None else history
        dates = [row["cal_date"] for row in self.calendar] if calendar_dates is None else calendar_dates
        db.scalars.side_effect = [_result(list(reversed(expected_history))), _result(dates)]
        counts = {day: 4900 for day in expected_history} if bar_counts is None else bar_counts
        adj = {day: 4900 for day in expected_history} if adj_counts is None else adj_counts
        db.execute.side_effect = [
            _result(list(counts.items())),
            _result([(self.latest, basic_count)]),
            _result(list(adj.items())),
        ]
        return db

    def catalog_responses(self):
        listed = [{"ts_code": f"{i:06d}.SZ", "name": f"Listed {i}"} for i in range(5000)]
        return {"L": {"rows": listed}, "P": {"rows": []},
                "D": {"rows": [{"ts_code": "600999.SH", "name": "Delisted"}]}}

    def mock_catalog(self, responses):
        self.market._tushare_request = Mock(side_effect=lambda api, params, fields: responses[params["list_status"]])

    def sync(self, *, partial=None, missing_bars=False, missing_adj=False, catalog_error=None, calendar=None):
        events = []
        responses = self.catalog_responses()
        if catalog_error:
            responses[catalog_error] = {"error": f"{catalog_error} upstream unavailable"}
        self.market.get_trade_cal = Mock(return_value={"rows": self.calendar if calendar is None else calendar})

        def request(api, params, fields):
            events.append((api, params.copy()))
            if api == "stock_basic":
                return responses[params["list_status"]]
            count = 1 if partial == api else 5000
            return {"rows": [{"ts_code": f"{i:06d}.SZ", "trade_date": params["trade_date"]}
                             for i in range(count)]}

        self.market._tushare_request = Mock(side_effect=request)
        self.market._bar_date_counts = Mock(return_value={} if missing_bars else {day: 5000 for day in self.history})
        self.market._adj_factor_date_counts = Mock(
            return_value={} if missing_adj else {day: 5000 for day in self.history}
        )
        writers = {}
        for name in ("_upsert_trading_calendar", "_upsert_instruments", "_upsert_daily_bars",
                     "_upsert_adj_factors", "_upsert_daily_basic", "_upsert_latest_prices"):
            def write(rows, *args, _name=name):
                events.append((_name, len(rows)))
                return len(rows)
            writers[name] = Mock(side_effect=write)
            setattr(self.market, name, writers[name])
        return events, writers

    def test_complete_cache_requires_all_twenty_one_sessions(self):
        db = self.cache()
        status = self.market.local_selection_cache_status()
        self.assertTrue(status["ready"], status["errors"])
        self.assertEqual(status["expected_trade_date"], self.latest)
        self.assertEqual(status["latest_trade_date"], self.latest)
        self.assertEqual(status["expected_history_dates"], self.history)
        self.assertEqual(len(status["history_date_counts"]), 21)
        self.assertEqual(status["catalog_count"], 5000)
        self.assertEqual(status["bar_coverage"], 0.98)
        self.assertEqual(status["basic_coverage"], 0.94)
        catalog_sql = _sql(db.scalar.call_args_list[-1].args[0])
        self.assertIn("instruments.list_status = 'L'", catalog_sql)
        for call in db.execute.call_args_list:
            self.assertIn("instruments.list_status = 'L'", _sql(call.args[0]))

    def test_one_current_row_is_not_a_full_market_cache(self):
        self.cache(bar_counts={day: 1 for day in self.history}, basic_count=1)
        status = self.market.local_selection_cache_status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["bar_count"], 1)
        self.assertEqual(status["basic_count"], 1)
        self.assertTrue(any("目标日日线覆盖不足" in error for error in status["errors"]))
        self.assertTrue(any("daily_basic 覆盖不足" in error for error in status["errors"]))

    def test_each_history_day_needs_adjustment_factor_coverage(self):
        adj = {day: 4900 for day in self.history}
        adj[self.history[7]] = 0
        self.cache(adj_counts=adj)
        status = self.market.local_selection_cache_status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["adj_factor_date_counts"][self.history[7]], 0)
        self.assertTrue(any("复权因子" in error and self.history[7] in error for error in status["errors"]))

    def test_each_expected_history_day_needs_full_coverage(self):
        for missing_count in (0, 1, 4249):
            with self.subTest(missing_count=missing_count):
                counts = {day: 4900 for day in self.history}
                counts[self.history[4]] = missing_count
                self.cache(bar_counts=counts)
                status = self.market.local_selection_cache_status()
                self.assertFalse(status["ready"])
                self.assertTrue(any(self.history[4] in error for error in status["errors"]))

    def test_exact_shared_thresholds_are_accepted(self):
        counts = {day: 4250 for day in self.history}
        counts[self.latest] = 4500
        self.cache(bar_counts=counts, basic_count=4250)
        status = self.market.local_selection_cache_status()
        self.assertTrue(status["ready"], status["errors"])
        self.assertEqual(status["minimum_counts"], {"bar": 4500, "basic": 4250, "history": 4250})
        self.quality["min_bar_coverage"] = 0.95
        self.assertEqual(self.market._selection_minimum_counts(5000)["bar"], 4750)

    def test_small_listed_catalog_cannot_make_coverage_look_complete(self):
        self.cache(catalog_count=1, bar_counts={day: 1 for day in self.history}, basic_count=1)
        status = self.market.local_selection_cache_status()
        self.assertFalse(status["ready"])
        self.assertTrue(any("目录不完整" in error for error in status["errors"]))

    def test_calendar_missing_today_does_not_fall_back_to_weekdays_or_old_watermark(self):
        self.cache(calendar_today=False)
        status = self.market.local_selection_cache_status()
        self.assertFalse(status["ready"])
        self.assertIsNone(status["expected_trade_date"])
        self.assertEqual(status["latest_trade_date"], self.latest)
        self.assertEqual(status["expected_history_dates"], [])
        self.assertTrue(any("未覆盖今日" in error for error in status["errors"]))

    def test_calendar_holes_are_not_silently_treated_as_holidays(self):
        gap = self.history[6]
        self.cache(calendar_dates=[row["cal_date"] for row in self.calendar if row["cal_date"] != gap])
        status = self.market.local_selection_cache_status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["calendar_missing_dates"], [gap])

    def test_calendar_gap_after_last_known_open_day_invalidates_target(self):
        self.cache(calendar_date="20260903", history=self.history[:-1],
                   calendar_dates=[row["cal_date"] for row in self.calendar if row["cal_date"] != self.latest])
        status = self.market.local_selection_cache_status()
        self.assertFalse(status["ready"])
        self.assertIsNone(status["expected_trade_date"])

    def test_stale_market_dates_are_reported_separately_from_expected_date(self):
        self.cache(bar_date="20260903", basic_date="20260902", basic_count=0,
                   bar_counts={day: 4900 for day in self.history[:-1]})
        status = self.market.local_selection_cache_status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["expected_trade_date"], self.latest)
        self.assertEqual(status["latest_trade_date"], "20260902")
        self.assertEqual(status["bar_date"], "20260903")
        self.assertEqual(status["basic_date"], "20260902")

    def test_history_window_length_is_not_satisfied_by_latest_day_only(self):
        self.cache(history=[self.latest])
        status = self.market.local_selection_cache_status(history_days=20)
        self.assertFalse(status["ready"])
        self.assertTrue(any("1/21" in error for error in status["errors"]))

    def test_custom_history_days_requests_one_extra_session(self):
        db = self.cache(history=self.history[-6:])
        status = self.market.local_selection_cache_status(history_days=5)
        self.assertTrue(status["ready"], status["errors"])
        self.assertEqual(len(status["expected_history_dates"]), 6)
        self.assertIn("LIMIT 6", _sql(db.scalars.call_args_list[0].args[0]))

    def test_history_loader_is_authoritative_inclusive_and_ascending(self):
        db = self.cache()
        db.scalars.side_effect = [_result(["20260904", "20260901"])]
        days = self.market.load_local_history_trade_dates(end_inclusive="2026-09-04", exchange="sse")
        self.assertEqual(days, ["20260901", "20260904"])
        sql = _sql(db.scalars.call_args.args[0])
        self.assertIn("cal_date <= '20260904'", sql)
        self.assertIn("source = 'tushare'", sql)
        self.assertIn("exchange = 'SSE'", sql)
        self.assertIn("cal_date DESC", sql)
        self.assertIn("LIMIT 21", sql)
        with self.assertRaises(ValueError):
            self.market.load_local_history_trade_dates(end_inclusive="not-a-date")

    def test_counts_deduplicate_symbols_and_use_raw_date_predicates(self):
        db = self.cache()
        db.execute.side_effect = [_result([("20260904", 4900)])]
        self.assertEqual(self.market._bar_date_counts([self.latest], listed_only=True), {self.latest: 4900})
        sql = _sql(db.execute.call_args.args[0])
        predicate = sql.split("WHERE", 1)[1].split("GROUP BY", 1)[0]
        self.assertNotIn("replace(", predicate.lower())
        self.assertIn("'2026-09-04'", predicate)
        self.assertIn("'20260904'", predicate)
        self.assertIn("count(distinct(daily_bars.symbol))", sql.lower())

    def test_latest_date_compares_compact_and_iso_ranges_without_replace(self):
        db = MagicMock()
        db.scalar.side_effect = ["20260831", "2026-09-04"]
        self.assertEqual(self.market._latest_local_date(db, DailyBar, self.today), self.latest)
        for call in db.scalar.call_args_list:
            sql = _sql(call.args[0])
            self.assertNotIn("replace(", sql.lower())
            self.assertIn("trade_date BETWEEN", sql)
            self.assertIn("length(daily_bars.trade_date)", sql)

    def test_delisted_historical_alias_cannot_collide_with_live_symbol(self):
        responses = self.catalog_responses()
        responses["L"]["rows"].append(
            {
                "ts_code": "600018.SH",
                "name": "上港集团",
                "list_status": "L",
            }
        )
        responses["D"] = {
            "rows": [
                {
                    "ts_code": "T600018.SH",
                    "name": "上港集箱(退)",
                    "list_status": "D",
                },
                {
                    "ts_code": "600999.SH",
                    "name": "标准退市标的",
                    "list_status": "D",
                },
            ]
        }
        self.mock_catalog(responses)

        rows = self.market._stock_catalog_rows()
        by_symbol = {row["ts_code"]: row for row in rows}

        self.assertEqual(by_symbol["600018.SH"]["list_status"], "L")
        self.assertEqual(by_symbol["600999.SH"]["list_status"], "D")
        self.assertNotIn("T600018.SH", by_symbol)
        self.assertEqual(sum(row["ts_code"] == "600018.SH" for row in rows), 1)

    def test_noncanonical_active_or_paused_symbol_is_a_hard_error(self):
        for status in ("L", "P"):
            with self.subTest(status=status):
                responses = self.catalog_responses()
                responses[status]["rows"] = [
                    {"ts_code": "T600018.SH", "list_status": status}
                ]
                self.mock_catalog(responses)
                with self.assertRaisesRegex(RuntimeError, rf"stock_basic\({status}\).*无效标的"):
                    self.market._stock_catalog_rows()

    def test_entirely_noncanonical_delisted_catalog_is_untrustworthy(self):
        responses = self.catalog_responses()
        responses["D"] = {
            "rows": [
                {
                    "ts_code": "T600018.SH",
                    "name": "上港集箱(退)",
                    "list_status": "D",
                }
            ]
        }
        self.mock_catalog(responses)

        with self.assertRaisesRegex(RuntimeError, "无标准六位股票代码"):
            self.market._stock_catalog_rows()

    def test_complete_catalog_fetches_l_p_d_and_allows_empty_p(self):
        self.mock_catalog(self.catalog_responses())
        rows = self.market._stock_catalog_rows()
        self.assertEqual(len(rows), 5001)
        self.assertEqual(rows[-1]["list_status"], "D")
        self.assertEqual([call.args[1]["list_status"] for call in self.market._tushare_request.call_args_list], ["L", "P", "D"])

    def test_catalog_propagates_every_channel_error_without_l_only_fallback(self):
        for status in ("L", "P", "D"):
            with self.subTest(status=status):
                responses = self.catalog_responses()
                responses[status] = {"error": "upstream unavailable"}
                self.mock_catalog(responses)
                with self.assertRaisesRegex(RuntimeError, rf"stock_basic\({status}\).*upstream unavailable"):
                    self.market._stock_catalog_rows()

    def test_empty_l_or_d_and_partial_l_are_untrustworthy(self):
        for status, rows in (("L", []), ("D", []), ("L", [{"ts_code": "000001.SZ"}])):
            with self.subTest(status=status, size=len(rows)):
                responses = self.catalog_responses()
                responses[status] = {"rows": rows}
                self.mock_catalog(responses)
                with self.assertRaises(RuntimeError):
                    self.market._stock_catalog_rows()

    def test_malformed_or_conflicting_catalog_never_becomes_a_snapshot(self):
        for delisted in ([{}], [{"ts_code": "000001.SZ"}], [{"ts_code": "600999.SH", "list_status": "L"}]):
            with self.subTest(delisted=delisted):
                responses = self.catalog_responses()
                responses["D"] = {"rows": delisted}
                self.mock_catalog(responses)
                with self.assertRaises(RuntimeError):
                    self.market.load_stock_catalog_snapshot()

    def test_cached_catalog_keeps_delisted_exclusion_evidence(self):
        db = self.cache()
        db.execute.side_effect = [_result([
            SimpleNamespace(symbol="600999.SH", name="Delisted", market="SH", type="stock",
                            list_status="D", list_date="19900101", industry="Other"),
        ])]
        rows = self.market.load_local_stock_catalog_snapshot()
        self.assertEqual(rows[0]["list_status"], "D")
        self.assertIn("list_status IN ('L', 'P', 'D')", _sql(db.execute.call_args.args[0]))

    def test_explicit_delisting_updates_only_confirmed_rows_not_absent_symbols(self):
        engine = MagicMock()
        conn = engine.begin.return_value.__enter__.return_value
        self.market.engine = engine
        self.assertEqual(self.market._upsert_instruments([{"ts_code": "600999.SH", "list_status": "D"}]), 1)
        statement, params = conn.execute.call_args.args
        self.assertEqual(params[0]["list_status"], "D")
        self.assertEqual(len(params), 1)
        sql = str(statement)
        self.assertIn("list_status=VALUES(list_status)", sql)
        self.assertNotIn("NOT IN", sql)
        self.assertNotIn("DELETE", sql)
        self.assertEqual(conn.execute.call_count, 1)

    def test_sync_fetches_complete_catalog_before_any_writes(self):
        events, writers = self.sync()
        status = self.market.sync_market(days=5)
        self.assertTrue(status["ready"], status["errors"])
        self.assertEqual([event[0] for event in events[:3]], ["stock_basic"] * 3)
        self.assertEqual([event[1]["list_status"] for event in events[:3]], ["L", "P", "D"])
        self.assertEqual(events[3][0], "_upsert_trading_calendar")
        self.assertEqual(writers["_upsert_instruments"].call_args.args[0][-1]["list_status"], "D")
        self.market.get_trade_cal.assert_called_once_with(start_date="20260508", end_date=self.today, exchange="SSE")

    def test_sync_catalog_error_makes_no_writes_including_calendar(self):
        for channel in ("L", "P", "D"):
            with self.subTest(channel=channel):
                _, writers = self.sync(catalog_error=channel)
                with self.assertRaisesRegex(RuntimeError, "upstream unavailable"):
                    self.market.sync_market(days=5)
                for writer in writers.values():
                    writer.assert_not_called()

    def test_sync_partial_daily_response_returns_errors_not_ready(self):
        _, writers = self.sync(partial="daily", missing_bars=True)
        status = self.market.sync_market(days=5)
        self.assertFalse(status["ready"])
        self.assertTrue(any("daily(" in error and "不完整" in error for error in status["errors"]))
        writers["_upsert_daily_bars"].assert_not_called()
        self.assertEqual(status["dates_fetched"], 0)

    def test_sync_backfills_adjustment_factors_for_every_missing_session(self):
        events, writers = self.sync(missing_adj=True)
        status = self.market.sync_market(days=5)
        self.assertTrue(status["ready"], status["errors"])
        adj_calls = [params["trade_date"] for api, params in events if api == "adj_factor"]
        self.assertEqual(adj_calls, self.history[-5:])
        self.assertEqual(status["adj_dates_fetched"], 5)
        self.assertEqual(status["adj_factors_upserted"], 25000)
        self.assertEqual(writers["_upsert_adj_factors"].call_count, 5)

    def test_sync_skips_sessions_whose_adjustment_factors_are_complete(self):
        events, writers = self.sync()
        status = self.market.sync_market(days=5)
        self.assertTrue(status["ready"], status["errors"])
        self.assertNotIn("adj_factor", [api for api, _ in events])
        writers["_upsert_adj_factors"].assert_not_called()

    def test_sync_partial_adjustment_factor_response_returns_errors_not_ready(self):
        _, writers = self.sync(partial="adj_factor", missing_adj=True)
        status = self.market.sync_market(days=5)
        self.assertFalse(status["ready"])
        self.assertTrue(any("adj_factor(" in error and "不完整" in error for error in status["errors"]))
        writers["_upsert_adj_factors"].assert_not_called()

    def test_adjustment_factor_upsert_drops_invalid_values(self):
        engine = MagicMock()
        conn = engine.begin.return_value.__enter__.return_value
        self.market.engine = engine
        rows = [
            {"ts_code": "600001.SH", "trade_date": "20260904", "adj_factor": 2.5},
            {"ts_code": "600002.SH", "trade_date": "2026-09-04", "adj_factor": "1.25"},
            {"ts_code": "600003.SH", "trade_date": "20260904", "adj_factor": 0},
            {"ts_code": "600004.SH", "trade_date": "20260904", "adj_factor": None},
            {"ts_code": "600005.SH", "trade_date": "20260904", "adj_factor": float("nan")},
            {"ts_code": "600006.SH", "trade_date": "bad", "adj_factor": 1.0},
        ]
        self.assertEqual(self.market._upsert_adj_factors(rows), 2)
        _, params = conn.execute.call_args.args
        self.assertEqual(params, [
            {"symbol": "600001.SH", "trade_date": "20260904", "adj_factor": 2.5},
            {"symbol": "600002.SH", "trade_date": "20260904", "adj_factor": 1.25},
        ])

    def test_sync_partial_basic_response_returns_errors_not_ready(self):
        _, writers = self.sync(partial="daily_basic")
        status = self.market.sync_market(days=5)
        self.assertFalse(status["ready"])
        self.assertTrue(any("daily_basic" in error for error in status["errors"]))
        writers["_upsert_daily_basic"].assert_not_called()

    def test_sync_partial_latest_refresh_is_not_success(self):
        self.sync(partial="daily")
        status = self.market.sync_market(days=5)
        self.assertFalse(status["ready"])
        self.assertTrue(any("latest(" in error for error in status["errors"]))
        self.assertEqual(status["latest_prices_upserted"], 0)

    def test_sync_rejects_stale_calendar_without_any_writes(self):
        _, writers = self.sync(calendar=self.calendar[:-1])
        with self.assertRaisesRegex(RuntimeError, "未覆盖今日"):
            self.market.sync_market(days=5)
        self.market._tushare_request.assert_not_called()
        for writer in writers.values():
            writer.assert_not_called()

    def test_sync_rejects_calendar_holes_and_ignores_future_rows(self):
        _, writers = self.sync(calendar=[row for row in self.calendar if row["cal_date"] != "20260903"])
        with self.assertRaisesRegex(RuntimeError, "缺失日期"):
            self.market.sync_market(days=5)
        for writer in writers.values():
            writer.assert_not_called()
        self.sync(calendar=[*self.calendar, {"exchange": "SSE", "cal_date": "20260907", "is_open": 1}])
        self.assertEqual(self.market.sync_market(days=5)["latest_trade_date"], self.latest)

    def test_market_response_duplicate_symbols_cannot_satisfy_count_guard(self):
        duplicate = {"ts_code": "000001.SZ", "trade_date": self.latest}
        with self.assertRaisesRegex(RuntimeError, "不完整"):
            self.market._validated_market_rows(
                {"rows": [duplicate] * 5000}, label="daily", trade_date=self.latest,
                minimum=3000, listed_symbols={"000001.SZ"},
            )

    def test_market_response_rejects_wrong_day_and_propagates_errors(self):
        for response in ({"rows": [{"ts_code": "000001.SZ", "trade_date": "20260903"}]},
                         {"error": "provider timeout"}, {"rows": []}):
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                self.market._validated_market_rows(response, label="daily", trade_date=self.latest,
                                                   minimum=3000, listed_symbols={"000001.SZ"})


if __name__ == "__main__":
    unittest.main()
