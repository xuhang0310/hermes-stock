"""技术信号：规则算得对不对、复权、翻转价、落库幂等、不用未来数据。

全部用内存 SQLite，不连 MySQL，不写生产库。
"""
from __future__ import annotations

import random
import unittest
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.models import Base, DailyBar, Position, PositionTrade, TechSignalRecord, WatchlistItem
from backend.app.quant.tech_signal import (
    FLAT,
    LONG,
    MIN_BARS,
    RULE_VERSION,
    UNKNOWN,
    Bar,
    adjusted_bars,
    compute_signal,
    macd_hist_series,
)
from backend.app.quant.tech_signal_service import load_bars, refresh_signals, signal_universe
from backend.app.scheduler import rhythm_scheduler


def _dates(count: int, start: date = date(2026, 1, 5)) -> list[str]:
    return [(start + timedelta(days=index)).strftime("%Y%m%d") for index in range(count)]


def _bars(closes: list[float], *, pre_close: bool = True) -> list[Bar]:
    bars = []
    for index, (day, close) in enumerate(zip(_dates(len(closes)), closes)):
        bars.append(
            Bar(
                trade_date=day,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                pre_close=closes[index - 1] if pre_close and index else None,
            )
        )
    return bars


def _walk(count: int, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    closes = [20.0]
    for _ in range(count - 1):
        closes.append(closes[-1] * (1 + rng.uniform(-0.03, 0.03)))
    return closes


def _trend(count: int, daily: float, start: float = 20.0) -> list[float]:
    return [start * (1 + daily) ** index for index in range(count)]


def _slide(count: int, step: float = -0.5, start: float = 100.0) -> list[float]:
    """每天跌同样多。不用按比例下跌：那样 DIF 的绝对值越缩越小，柱子反而转正（阴跌亮红）。"""
    return [start + step * index for index in range(count)]


class RuleTests(unittest.TestCase):
    def test_macd_matches_the_textbook_formula(self) -> None:
        closes = _walk(200)
        series = pd.Series(closes)
        dif = series.ewm(span=12, adjust=False).mean() - series.ewm(span=26, adjust=False).mean()
        dea = dif.ewm(span=9, adjust=False).mean()
        expected = (2 * (dif - dea)).tolist()
        for mine, theirs in zip(macd_hist_series(closes), expected):
            self.assertAlmostEqual(mine, theirs, places=9)

    def test_rising_is_red_and_falling_is_green(self) -> None:
        self.assertEqual(compute_signal(_bars(_trend(90, 0.01))).direction, LONG)
        self.assertEqual(compute_signal(_bars(_slide(90))).direction, FLAT)

    def test_a_slow_proportional_decline_can_show_red(self) -> None:
        # MACD 的已知特点，记在这里：规则照算，不做特殊处理
        self.assertEqual(compute_signal(_bars(_trend(90, -0.01))).direction, LONG)

    def test_a_flat_price_is_green_because_red_needs_a_positive_bar(self) -> None:
        signal = compute_signal(_bars([10.0] * 90))
        self.assertEqual(signal.direction, FLAT)
        self.assertEqual(signal.rsi14, 50.0)

    def test_too_few_bars_gives_no_direction(self) -> None:
        signal = compute_signal(_bars(_trend(MIN_BARS - 1, 0.01)))
        self.assertEqual(signal.direction, UNKNOWN)
        self.assertIsNone(signal.macd_hist)
        self.assertIsNone(signal.flip_price)
        self.assertEqual(compute_signal(_bars(_trend(MIN_BARS, 0.01))).direction, LONG)

    def test_no_bars_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            compute_signal([])

    def test_the_flip_day_and_days_in_state_are_tracked(self) -> None:
        closes = _slide(70)
        for _ in range(6):
            closes.append(closes[-1] * 1.05)
        hist = macd_hist_series(closes)
        first_red = next(index for index in range(70, len(hist)) if hist[index] > 0)
        bars = _bars(closes)

        on_the_day = compute_signal(bars[: first_red + 1])
        self.assertEqual(on_the_day.direction, LONG)
        self.assertTrue(on_the_day.flipped)
        self.assertEqual(on_the_day.days_in_state, 1)
        self.assertEqual(on_the_day.flip_date, bars[first_red].trade_date)

        two_days_later = compute_signal(bars[: first_red + 3])
        self.assertFalse(two_days_later.flipped)
        self.assertEqual(two_days_later.days_in_state, 3)
        self.assertEqual(two_days_later.flip_date, bars[first_red].trade_date)

    def test_the_flip_price_is_where_the_colour_changes_tomorrow(self) -> None:
        for closes, before in ((_trend(90, 0.01), LONG), (_slide(90), FLAT), (_walk(120), None)):
            bars = _bars(closes)
            signal = compute_signal(bars)
            if before:
                self.assertEqual(signal.direction, before)
            self.assertIsNotNone(signal.flip_price)

            def tomorrow(close: float) -> str:
                return compute_signal(bars + [Bar("20270101", close, close, close, bars[-1].close)]).direction

            self.assertEqual(tomorrow(signal.flip_price * 1.0005), LONG)
            self.assertEqual(tomorrow(signal.flip_price * 0.9995), FLAT)


class AdjustmentTests(unittest.TestCase):
    def _with_split(self, *, pre_close: bool) -> list[Bar]:
        """倒数第 5 天 10 转 10：价格减半，当天 pre_close 也减半。"""
        closes = _trend(90, 0.01)
        split = 85
        halved = [close if index < split else close / 2 for index, close in enumerate(closes)]
        bars = []
        for index, (day, close) in enumerate(zip(_dates(len(closes)), halved)):
            previous = closes[index - 1] / 2 if index >= split else (closes[index - 1] if index else None)
            bars.append(Bar(day, close, close, close, previous if pre_close else None))
        return bars

    def test_a_split_does_not_look_like_a_crash(self) -> None:
        plain = compute_signal(_bars(_trend(90, 0.01)))
        split = compute_signal(self._with_split(pre_close=True))
        self.assertEqual(split.direction, plain.direction)
        self.assertEqual(split.days_in_state, plain.days_in_state)
        self.assertAlmostEqual(split.macd_hist, plain.macd_hist / 2, places=6)
        self.assertAlmostEqual(split.ma20, plain.ma20 / 2, places=6)
        self.assertAlmostEqual(split.flip_price, plain.flip_price / 2, places=6)

    def test_without_adjustment_the_same_split_would_turn_green(self) -> None:
        self.assertEqual(compute_signal(self._with_split(pre_close=False)).direction, FLAT)

    def test_the_last_bar_keeps_its_real_price(self) -> None:
        bars = self._with_split(pre_close=True)
        self.assertEqual(compute_signal(bars).close, bars[-1].close)
        self.assertEqual(adjusted_bars(bars)[-1].close, bars[-1].close)

    def test_a_dirty_pre_close_is_not_treated_as_a_split(self) -> None:
        bars = _bars(_trend(90, 0.01))
        dirty = list(bars)
        dirty[50] = Bar(bars[50].trade_date, bars[50].high, bars[50].low, bars[50].close, 0.0001)
        self.assertEqual(compute_signal(dirty).macd_hist, compute_signal(bars).macd_hist)


class _DbCase(unittest.TestCase):
    NOW = datetime(2026, 11, 5, 10, 0)

    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)

    def _add_bars(self, db, symbol: str, closes: list[float], *, source: str = "tushare") -> list[str]:
        days = _dates(len(closes), date(2026, 6, 1))
        for index, (day, close) in enumerate(zip(days, closes)):
            db.add(
                DailyBar(
                    symbol=symbol,
                    trade_date=day,
                    open=close,
                    high=close * 1.01,
                    low=close * 0.99,
                    close=close,
                    pre_close=closes[index - 1] if index else None,
                    source=source,
                )
            )
        db.commit()
        return days

    def _rows(self, db) -> list[TechSignalRecord]:
        return list(db.scalars(select(TechSignalRecord).order_by(TechSignalRecord.symbol)).all())


class UniverseTests(_DbCase):
    def _trade(self, db, symbol: str, action: str, *, days_ago: int = 1, source: str = "manual") -> None:
        db.add(
            PositionTrade(
                symbol=symbol,
                action=action,
                source_type=source,
                created_at=self.NOW - timedelta(days=days_ago),
            )
        )

    def test_holdings_watchlist_and_recent_sells_are_covered(self) -> None:
        with self.Session() as db:
            db.add_all(
                [
                    Position(symbol="HELD", position_pct=0, quantity=100, status="open"),
                    Position(symbol="CLOSED", position_pct=0, quantity=0, status="closed"),
                    Position(symbol="ZERO", position_pct=0, quantity=0, status="open"),
                    WatchlistItem(symbol="WATCH", group_id=1, status="active"),
                    WatchlistItem(symbol="GONE", group_id=1, status="removed"),
                ]
            )
            self._trade(db, "SOLD", "sell")
            self._trade(db, "REDUCED", "reduce")
            self._trade(db, "OLD", "sell", days_ago=91)
            self._trade(db, "BOUGHT", "buy")
            self._trade(db, "IMPORTED", "sell", source="screenshot_import")
            db.commit()
            self.assertEqual(signal_universe(db, now=self.NOW), ["HELD", "REDUCED", "SOLD", "WATCH"])


class RefreshTests(_DbCase):
    def test_signals_are_stored_with_the_rule_version(self) -> None:
        with self.Session() as db:
            days = self._add_bars(db, "AAA", _trend(90, 0.01))
            summary = refresh_signals(db, trade_date=days[-1], symbols=["AAA"])
            rows = self._rows(db)
        self.assertEqual(summary["stored"], 1)
        self.assertEqual(summary["long"], 1)
        self.assertEqual(summary["rule_version"], RULE_VERSION)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].direction, rows[0].rule_version, rows[0].trade_date), (LONG, RULE_VERSION, days[-1]))
        self.assertAlmostEqual(rows[0].close, _trend(90, 0.01)[-1], places=3)

    def test_running_twice_updates_the_same_row(self) -> None:
        closes = _trend(90, 0.01)
        with self.Session() as db:
            days = self._add_bars(db, "AAA", closes)
            refresh_signals(db, trade_date=days[-1], symbols=["AAA"])
            bar = db.scalar(select(DailyBar).where(DailyBar.symbol == "AAA", DailyBar.trade_date == days[-1]))
            bar.close = 10.0
            db.commit()
            refresh_signals(db, trade_date=days[-1], symbols=["AAA"])
            rows = self._rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].close, 10.0)
        self.assertEqual(rows[0].direction, FLAT)

    def test_a_past_date_only_uses_bars_up_to_that_day(self) -> None:
        closes = _trend(70, 0.01) + _trend(20, -0.05, start=_trend(70, 0.01)[-1])
        with self.Session() as db:
            days = self._add_bars(db, "AAA", closes)
            refresh_signals(db, trade_date=days[69], symbols=["AAA"])
            row = self._rows(db)[0]
        self.assertEqual(row.trade_date, days[69])
        self.assertEqual(row.direction, LONG)
        self.assertEqual(row.bars_used, 70)

    def test_a_stock_without_todays_bar_is_listed_and_not_written(self) -> None:
        with self.Session() as db:
            days = self._add_bars(db, "AAA", _trend(90, 0.01))
            self._add_bars(db, "STOPPED", _trend(80, 0.01))
            summary = refresh_signals(db, trade_date=days[-1], symbols=["AAA", "STOPPED", "NODATA"])
            rows = self._rows(db)
        self.assertEqual(summary["missing"], ["NODATA", "STOPPED"])
        self.assertEqual([row.symbol for row in rows], ["AAA"])

    def test_too_little_history_is_stored_as_unknown(self) -> None:
        with self.Session() as db:
            days = self._add_bars(db, "NEW", _trend(20, 0.01))
            summary = refresh_signals(db, trade_date=days[-1], symbols=["NEW"])
            rows = self._rows(db)
        self.assertEqual(summary["unknown"], 1)
        self.assertEqual(rows[0].direction, UNKNOWN)

    def test_a_flip_is_reported_in_the_summary(self) -> None:
        closes = _slide(70)
        for _ in range(6):
            closes.append(closes[-1] * 1.05)
        first_red = next(index for index in range(70, len(closes)) if macd_hist_series(closes)[index] > 0)
        with self.Session() as db:
            days = self._add_bars(db, "AAA", closes)
            summary = refresh_signals(db, trade_date=days[first_red], symbols=["AAA"])
        self.assertEqual(summary["flipped"], [{"symbol": "AAA", "direction": LONG}])

    def test_the_date_may_be_written_with_dashes(self) -> None:
        with self.Session() as db:
            days = self._add_bars(db, "AAA", _trend(90, 0.01))
            dashed = f"{days[-1][:4]}-{days[-1][4:6]}-{days[-1][6:]}"
            self.assertEqual(refresh_signals(db, trade_date=dashed, symbols=["AAA"])["stored"], 1)

    def test_the_universe_is_used_when_no_symbols_are_given(self) -> None:
        with self.Session() as db:
            days = self._add_bars(db, "AAA", _trend(90, 0.01))
            db.add(Position(symbol="AAA", position_pct=0, quantity=100, status="open"))
            db.commit()
            self.assertEqual(refresh_signals(db, trade_date=days[-1], now=self.NOW)["stored"], 1)

    def test_only_one_source_is_used_for_a_day(self) -> None:
        closes = _trend(90, 0.01)
        with self.Session() as db:
            days = self._add_bars(db, "AAA", closes)
            self._add_bars(db, "AAA", [close * 9 for close in closes], source="other")
            bars = load_bars(db, ["AAA"], days[-1])["AAA"]
        self.assertEqual(len(bars), 90)
        self.assertAlmostEqual(bars[-1].close, closes[-1], places=3)


class NotLiveYetTests(unittest.TestCase):
    """MACD 规则没通过全市场验证（docs/technical-signal-plan.md 第 4 节），不能接进流程。"""

    def test_the_scheduler_does_not_refresh_signals(self) -> None:
        self.assertFalse(hasattr(rhythm_scheduler, "_refresh_tech_signals"))

    def test_digital_humans_cannot_read_the_signals(self) -> None:
        from backend.app.agents.sql_sandbox import SqlRejected, prepare_query

        with self.assertRaises(SqlRejected):
            prepare_query("SELECT symbol, direction FROM tech_signals", owner_user_id=1, max_rows=10)


if __name__ == "__main__":
    unittest.main()
