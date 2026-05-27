"""
Today KRX Macro Rebound

매수 조건: 단기 상승 전환 + 20일 EMA 위 + RSI 중립/강세 + 거래량 확인
매도 조건: 당일 -3% 이상 하락 또는 20일 EMA 하회와 RSI 약세 동시 발생
"""

from core import data_fetcher, indicators
from core.signal import Action, Signal
from strategy.base_strategy import BaseStrategy


class TodayKrxMacroReboundStrategy(BaseStrategy):
    """뉴스 기반 한국장 단기 반등 필터."""

    @property
    def name(self) -> str:
        return "오늘 한국장 매크로 반등"

    @property
    def required_days(self) -> int:
        return 45

    def generate_signal(self, stock_code: str, stock_name: str) -> Signal:
        df = data_fetcher.get_daily_prices(stock_code, self.required_days)

        if df.empty or len(df) < 25:
            return Signal(
                stock_code=stock_code,
                stock_name=stock_name,
                action=Action.HOLD,
                strength=0.0,
                reason="데이터 부족",
            )

        close = float(df["close"].iloc[-1])
        prev_close = float(df["close"].iloc[-2])
        high = float(df["high"].iloc[-1])
        low = float(df["low"].iloc[-1])
        volume = float(df["volume"].iloc[-1])

        ema20 = float(indicators.calc_ema(df, 20).iloc[-1])
        rsi14 = float(indicators.calc_rsi(df, 14).iloc[-1])
        volume_ma20 = float(indicators.calc_volume_ma(df, 20).iloc[-1])
        one_day_return = ((close / prev_close) - 1.0) * 100 if prev_close > 0 else 0.0
        ema_gap = ((close / ema20) - 1.0) * 100 if ema20 > 0 else 0.0
        close_ratio = (close - low) / (high - low) if high > low else 0.5
        volume_ratio = volume / volume_ma20 if volume_ma20 > 0 else 1.0

        if one_day_return <= -3.0:
            return Signal(
                stock_code=stock_code,
                stock_name=stock_name,
                action=Action.SELL,
                strength=0.8,
                reason=f"당일 급락 {one_day_return:.1f}%로 리스크 축소",
                target_price=int(close),
            )

        if close < ema20 and rsi14 < 45:
            return Signal(
                stock_code=stock_code,
                stock_name=stock_name,
                action=Action.SELL,
                strength=0.72,
                reason=f"EMA20 하회 + RSI {rsi14:.1f} 약세",
                target_price=int(close),
            )

        buy_signal = (
            one_day_return > 0
            and close > ema20
            and 45 <= rsi14 <= 76
            and close_ratio >= 0.55
        )

        if buy_signal:
            strength = 0.62
            strength += min(0.14, max(0.0, one_day_return) / 25)
            strength += min(0.10, max(0.0, ema_gap) / 40)
            strength += min(0.08, max(0.0, close_ratio - 0.55) * 0.18)
            strength += min(0.06, max(0.0, volume_ratio - 0.75) * 0.08)
            strength = min(0.95, max(0.7, strength))

            return Signal(
                stock_code=stock_code,
                stock_name=stock_name,
                action=Action.BUY,
                strength=round(strength, 2),
                reason=(
                    f"반등 필터 충족: 등락 {one_day_return:+.1f}%, "
                    f"EMA20 대비 {ema_gap:+.1f}%, RSI {rsi14:.1f}, "
                    f"종가위치 {close_ratio*100:.0f}%, 거래량 {volume_ratio:.1f}배"
                ),
                target_price=int(close),
            )

        return Signal(
            stock_code=stock_code,
            stock_name=stock_name,
            action=Action.HOLD,
            strength=0.0,
            reason=(
                f"조건 미충족: 등락 {one_day_return:+.1f}%, "
                f"EMA20 대비 {ema_gap:+.1f}%, RSI {rsi14:.1f}, "
                f"종가위치 {close_ratio*100:.0f}%, 거래량 {volume_ratio:.1f}배"
            ),
            target_price=int(close),
        )
