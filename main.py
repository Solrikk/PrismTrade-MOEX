import os
from datetime import datetime, timedelta
import numpy as np
import matplotlib.pyplot as plt
from tinkoff.invest import Client, RequestError, CandleInterval
import pytz
from tinkoff.invest.utils import now
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler
import matplotlib
matplotlib.use('Agg')
import json
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn

ENSEMBLE_WEIGHTS_HIGH_VOL = [0.2, 0.3, 0.5]
ENSEMBLE_WEIGHTS_LOW_VOL = [0.4, 0.3, 0.3]

app = FastAPI(title="PrismTrade")
if not os.path.exists('templates'):
    os.makedirs('templates')
if not os.path.exists('static'):
    os.makedirs('static')
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

class StockPredictor:
    def __init__(self, ticker=None):
        self.token = os.getenv('TINKOFF_TOKEN')
        if not self.token:
            raise ValueError("TINKOFF_TOKEN не настроен. Пожалуйста, добавьте токен в Secrets (Tools -> Secrets)")
        self.ticker = ticker
        self.figi = None

    def set_ticker(self, ticker):
        try:
            with Client(self.token) as client:
                instruments = client.instruments.find_instrument(query=ticker)
                for instrument in instruments.instruments:
                    if instrument.ticker == ticker and instrument.class_code == 'TQBR':
                        self.figi = instrument.figi
                        self.ticker = ticker
                        return True
                print(f"❌ Тикер {ticker} не найден")
                return False
        except Exception as e:
            print(f"Ошибка при поиске тикера: {e}")
            return False

    def get_recommendation(self, rsi, macd, signal, price_change, momentum, current_price):
        score = 0
        reasons = []
        has_market_state = hasattr(self, 'last_market_state')
        market_state = getattr(self, 'last_market_state', {})
        is_bullish = market_state.get('bullish', False) if has_market_state else False
        is_bearish = market_state.get('bearish', False) if has_market_state else False
        is_correction = market_state.get('correction', False) if has_market_state else False
        is_pullback_opportunity = market_state.get('pullback_opportunity', False) if has_market_state else False
        trend_strength = market_state.get('trend_strength', 50) if has_market_state else 50
        if is_bullish:
            reasons.append(f"Установлен бычий тренд (сила: {trend_strength}%)")
        if is_bearish:
            reasons.append(f"Установлен медвежий тренд (сила: {trend_strength}%)")
        if is_correction:
            correction_depth = market_state.get('correction_depth', 0)
            reasons.append(f"Обнаружена коррекция в рамках тренда (глубина: {correction_depth:.2f}%)")
        if is_pullback_opportunity:
            reasons.append("Хорошая возможность входа на откате")
        if rsi < 30:
            if is_bullish:
                score += 4
                reasons.append("RSI показывает перепроданность в бычьем тренде (очень сильный сигнал к покупке)")
            else:
                score += 3
                reasons.append("RSI показывает перепроданность (сильный сигнал к покупке)")
        elif rsi < 40:
            if is_bullish:
                score += 2
                reasons.append("RSI ниже нормы в бычьем тренде (умеренный сигнал к покупке)")
            else:
                score += 1
                reasons.append("RSI ниже нормы (умеренный сигнал к покупке)")
        elif rsi > 70:
            if is_bearish:
                score -= 3
                reasons.append("RSI показывает перекупленность в медвежьем тренде (сильный сигнал к продаже)")
            else:
                score -= 2
                reasons.append("RSI показывает перекупленность (сигнал к продаже)")
            if is_bullish and is_correction:
                score += 2
                reasons.append("Высокий RSI в рамках коррекции бычьего тренда (игнорируем сигнал к продаже)")
        elif rsi > 60:
            if is_bearish:
                score -= 2
                reasons.append("RSI выше нормы в медвежьем тренде (умеренный сигнал к продаже)")
            else:
                score -= 1
                reasons.append("RSI выше нормы (слабый сигнал к продаже)")
            if is_bullish:
                score += 1
                reasons.append("Умеренно высокий RSI в бычьем тренде (нейтрализуем сигнал)")
        macd_diff = macd - signal
        if macd > signal:
            macd_strength = min(3, 1 + abs(macd_diff) * 5)
            if is_bullish:
                score += macd_strength + 1
                reasons.append(f"MACD выше сигнальной линии в бычьем тренде (сильный сигнал к покупке)")
            else:
                score += macd_strength
                reasons.append(f"MACD выше сигнальной линии (сигнал к покупке, сила: {macd_strength:.1f})")
        else:
            macd_strength = min(2, 0.5 + abs(macd_diff) * 3)
            if is_bullish and is_correction:
                score -= macd_strength * 0.3
                reasons.append("MACD ниже сигнальной линии в коррекции бычьего тренда (слабый сигнал игнорируем)")
            elif is_bearish:
                score -= macd_strength + 0.5
                reasons.append(f"MACD ниже сигнальной линии в медвежьем тренде (сигнал к продаже)")
            else:
                score -= macd_strength * 0.7
                reasons.append(f"MACD ниже сигнальной линии (слабый сигнал к продаже)")
        if is_bullish and is_correction and price_change < 0:
            score += 2
            reasons.append("Коррекция в бычьем тренде (хорошая возможность для покупки)")
        elif is_bearish and is_correction and price_change > 0:
            score -= 2
            reasons.append("Коррекция в медвежьем тренде (возможность для продажи)")
        else:
            if price_change > 1.5:
                if is_bearish and not is_correction:
                    score += 1
                    reasons.append("Положительная динамика цены > 1.5% в медвежьем тренде (возможен отскок)")
                else:
                    score += 3
                    reasons.append("Положительная динамика цены > 1.5% (сильный сигнал к покупке)")
            elif price_change > 0:
                if is_bullish:
                    score += 2
                    reasons.append("Положительная динамика цены в бычьем тренде (усиленный сигнал к покупке)")
                else:
                    score += 1
                    reasons.append("Положительная динамика цены (слабый сигнал к покупке)")
            elif price_change < -2.0:
                if is_bullish and is_correction:
                    score += 2
                    reasons.append("Отрицательная динамика > 2.0% как коррекция в бычьем тренде (возможность для покупки)")
                else:
                    score -= 2
                    reasons.append("Отрицательная динамика цены < -2.0% (сигнал к продаже)")
            elif price_change < 0:
                if is_bullish and is_correction:
                    score += 1
                    reasons.append("Отрицательная динамика в рамках коррекции бычьего тренда (возможность для покупки)")
                else:
                    score -= 1
                    reasons.append("Отрицательная динамика цены (слабый сигнал к продаже)")
        if momentum > 3:
            score += 2
            reasons.append("Сильный положительный моментум (сигнал к покупке)")
        elif momentum > 1:
            score += 1
            reasons.append("Положительный моментум (слабый сигнал к покупке)")
        elif momentum < -4:
            if is_bullish and is_correction:
                score += 1
                reasons.append("Отрицательный моментум в коррекции бычьего тренда (возможность для покупки)")
            else:
                score -= 2
                reasons.append("Сильный отрицательный моментум (сигнал к продаже)")
        elif momentum < -2:
            if is_bullish and is_correction:
                score += 0.5
                reasons.append("Умеренный отрицательный моментум в коррекции бычьего тренда (нейтральный сигнал)")
            else:
                score -= 1
                reasons.append("Отрицательный моментум (слабый сигнал к продаже)")
        if hasattr(self, 'ma5') and hasattr(self, 'ma20'):
            if self.ma5 > self.ma20:
                ma_diff = (self.ma5 / self.ma20 - 1) * 100
                ma_score = min(3, 1 + ma_diff * 0.5)
                score += ma_score
                reasons.append(f"Восходящий тренд по MA (MA5 > MA20, расхождение: {ma_diff:.2f}%, сигнал к покупке)")
            else:
                ma_diff = (self.ma20 / self.ma5 - 1) * 100
                ma_score = min(2, 0.5 + ma_diff * 0.4)
                if is_bullish and is_correction:
                    score -= ma_score * 0.3
                    reasons.append("MA5 < MA20 в коррекции бычьего тренда (слабый сигнал)")
                else:
                    score -= ma_score
                    reasons.append(f"Нисходящий тренд по MA (MA5 < MA20, расхождение: {ma_diff:.2f}%, сигнал к продаже)")
        if is_pullback_opportunity:
            score += 2
            reasons.append("Обнаружена хорошая возможность для входа на откате")
        if trend_strength > 70:
            if is_bullish:
                score += 2
                reasons.append(f"Очень сильный бычий тренд (сила: {trend_strength}%)")
            elif is_bearish:
                score -= 2
                reasons.append(f"Очень сильный медвежий тренд (сила: {trend_strength}%)")
        entry_exit_prices = self.calculate_entry_exit_prices(current_price, volatility=price_change)
        context_coefficient = trend_strength / 100
        if is_bullish:
            trend_score_adjustment = 0.5 + (context_coefficient * 1.5)
            score += trend_score_adjustment
            reasons.append(f"Корректировка на силу бычьего тренда: +{trend_score_adjustment:.2f}")
        elif is_bearish:
            trend_score_adjustment = 0.5 + (context_coefficient * 1.5)
            score -= trend_score_adjustment
            reasons.append(f"Корректировка на силу медвежьего тренда: -{trend_score_adjustment:.2f}")
        if score >= 3:
            return "ПОКУПАТЬ (ЛОНГ) - Сильный сигнал", reasons, entry_exit_prices
        elif score > 0:
            return "ПОКУПАТЬ (ЛОНГ) - Слабый сигнал", reasons, entry_exit_prices
        elif score > -3:
            return "ПРОДАВАТЬ (ШОРТ) - Слабый сигнал", reasons, entry_exit_prices
        else:
            return "ПРОДАВАТЬ (ШОРТ) - Сильный сигнал", reasons, entry_exit_prices

    def calculate_entry_exit_prices(self, current_price, volatility):
        has_market_state = hasattr(self, 'last_market_state')
        market_state = getattr(self, 'last_market_state', {})
        is_bullish = market_state.get('bullish', False) if has_market_state else False
        is_bearish = market_state.get('bearish', False) if has_market_state else False
        is_correction = market_state.get('correction', False) if has_market_state else False
        is_pullback_opportunity = market_state.get('pullback_opportunity', False) if has_market_state else False
        trend_strength = market_state.get('trend_strength', 50) if has_market_state else 50
        min_profit_pct_buy = 1.0
        min_profit_pct_sell = 0.5
        if is_bullish:
            trend_bonus = trend_strength / 100 * 1.5
            min_profit_pct_buy = min_profit_pct_buy + trend_bonus
            min_profit_pct_sell = max(0.3, min_profit_pct_sell - trend_bonus * 0.3)
        elif is_bearish:
            trend_bonus = trend_strength / 100 * 1.2
            min_profit_pct_sell = min_profit_pct_sell + trend_bonus
            min_profit_pct_buy = max(0.5, min_profit_pct_buy - trend_bonus * 0.3)
        entry_adjustment = 1.0
        if is_bullish and is_correction:
            entry_adjustment = 0.5 + (market_state.get('correction_depth', 0) / 10)
            min_profit_pct_buy = min_profit_pct_buy * 1.2
        volatility_coefficient_buy = 1.5
        volatility_coefficient_sell = 1.0
        if is_bullish:
            volatility_coefficient_buy = volatility_coefficient_buy + (trend_strength / 100 * 0.5)
            volatility_coefficient_sell = volatility_coefficient_sell - (trend_strength / 100 * 0.3)
        elif is_bearish:
            volatility_coefficient_buy = volatility_coefficient_buy - (trend_strength / 100 * 0.3)
            volatility_coefficient_sell = volatility_coefficient_sell + (trend_strength / 100 * 0.5)
        min_price_change_pct_buy = min_profit_pct_buy
        min_price_change_pct_sell = min_profit_pct_sell
        if hasattr(self, 'last_volatility'):
            real_volatility = self.last_volatility
        else:
            real_volatility = abs(volatility)
        if real_volatility < 0.8:
            target_price_change_pct_buy = max(min_price_change_pct_buy, 1.2)
            target_price_change_pct_sell = max(min_price_change_pct_sell, 1.5)
        elif real_volatility < 1.5:
            target_price_change_pct_buy = max(min_price_change_pct_buy, 1.8)
            target_price_change_pct_sell = max(min_price_change_pct_sell, 2.0)
        else:
            target_price_change_pct_buy = max(min_price_change_pct_buy, real_volatility * volatility_coefficient_buy)
            target_price_change_pct_sell = max(min_price_change_pct_sell, real_volatility * volatility_coefficient_sell)
        if is_bullish:
            target_price_change_pct_buy = min(target_price_change_pct_buy, 6.0)
            target_price_change_pct_sell = min(target_price_change_pct_sell, 2.5)
        elif is_bearish:
            target_price_change_pct_buy = min(target_price_change_pct_buy, 3.0)
            target_price_change_pct_sell = min(target_price_change_pct_sell, 4.0)
        else:
            target_price_change_pct_buy = min(target_price_change_pct_buy, 4.5)
            target_price_change_pct_sell = min(target_price_change_pct_sell, 3.5)
        if is_bullish and is_pullback_opportunity:
            entry_price_buy = current_price * (0.9990 - entry_adjustment * 0.001)
        else:
            entry_price_buy = current_price * 0.9990
        exit_price_buy = current_price * (1 + target_price_change_pct_buy / 100)
        if is_bearish and is_correction and is_pullback_opportunity:
            entry_price_sell = current_price * (1.0015 + entry_adjustment * 0.001)
        else:
            entry_price_sell = current_price * 1.0015
        exit_price_sell = current_price * (1 - target_price_change_pct_sell / 100)
        if is_bullish:
            stop_loss_percent_buy = min(target_price_change_pct_buy / 3.5, 1.0)
            stop_loss_percent_sell = min(target_price_change_pct_sell / 2.0, 2.0)
        elif is_bearish:
            stop_loss_percent_buy = min(target_price_change_pct_buy / 2.5, 1.8)
            stop_loss_percent_sell = min(target_price_change_pct_sell / 3.0, 1.2)
        else:
            stop_loss_percent_buy = min(target_price_change_pct_buy / 3, 1.2)
            stop_loss_percent_sell = min(target_price_change_pct_sell / 2.5, 1.5)
        if is_bullish and is_correction:
            stop_loss_percent_buy = stop_loss_percent_buy * 0.8
        elif is_bearish and is_correction:
            stop_loss_percent_sell = stop_loss_percent_sell * 0.8
        stop_loss_buy = current_price * (1 - stop_loss_percent_buy / 100)
        stop_loss_sell = current_price * (1 + stop_loss_percent_sell / 100)
        holding_period = 1
        if is_bullish:
            holding_period = max(1, int(2 + (trend_strength / 20)))
        elif is_bearish:
            holding_period = max(1, int(1.5 + (trend_strength / 25)))
        if real_volatility < 1.0:
            holding_period = int(holding_period * 1.5)
        return {
            "entry_price_buy": entry_price_buy,
            "exit_price_buy": exit_price_buy,
            "stop_loss_buy": stop_loss_buy,
            "entry_price_sell": entry_price_sell,
            "exit_price_sell": exit_price_sell,
            "stop_loss_sell": stop_loss_sell
        }

    def calculate_volatility(self, prices):
        if len(prices) < 2:
            return 0.0
        returns = pd.Series(prices).pct_change().dropna()
        if returns.empty:
            return 0.0
        return returns.std() * np.sqrt(252)

    def calculate_momentum(self, prices, period=14):
        if len(prices) <= period:
            period = max(1, len(prices) - 1)
        long_momentum = (prices[-1] / prices[-period] - 1) * 100
        short_period = min(7, len(prices) // 3)
        if short_period < 1:
            short_period = 1
        short_momentum = (prices[-1] / prices[-short_period] - 1) * 100 if short_period > 0 else 0
        combined_momentum = (short_momentum * 0.7) + (long_momentum * 0.3)
        return combined_momentum

    def collect_data(self, hours=24):
        print("Получение данных из Тинькофф...")
        moscow_tz = pytz.timezone('Europe/Moscow')
        current_time = datetime.now(moscow_tz)
        times = []
        prices = []
        volumes = []
        try:
            with Client(self.token) as client:
                moscow_tz = pytz.timezone('Europe/Moscow')
                current_time = datetime.now(moscow_tz)
                from_ = current_time - timedelta(hours=hours)
                to = current_time
                candles = client.market_data.get_candles(
                    figi=self.figi,
                    from_=from_,
                    to=to,
                    interval=CandleInterval.CANDLE_INTERVAL_5_MIN).candles
                print(f"Получено {len(candles)} свечей за последние {hours} часов")
                if not candles:
                    print("Не удалось получить данные о свечах")
                    return [], [], []
                candles = sorted(candles, key=lambda x: x.time)
                for candle in candles:
                    moscow_time = candle.time.astimezone(moscow_tz)
                    times.append(moscow_time)
                    price = float(candle.close.units) + float(candle.close.nano) / 1e9
                    prices.append(price)
                    volumes.append(candle.volume)
                if len(prices) < 20:
                    print(f"Недостаточно данных для анализа. Получено точек: {len(prices)}, требуется минимум 20")
                    print("Возможно, торги еще не начались или временно приостановлены")
                    return [], [], []
                if times:
                    current_time = datetime.now(moscow_tz).replace(microsecond=0)
                    last_candle_time = times[-1].replace(microsecond=0)
                    print(f"Текущее время сервера: {current_time.strftime('%d.%m.%Y %H:%M')}")
                    if last_candle_time > current_time:
                        print(f"⚠️ Ошибка: Некорректное время данных (будущее время)")
                        print(f"Последнее время свечи: {last_candle_time.strftime('%d.%m.%Y %H:%M')}")
                        print(f"Текущее время: {current_time.strftime('%d.%m.%Y %H:%M')}")
                        return [], [], []
                    time_diff = current_time - last_candle_time
                    print(f"\nПоследнее обновление данных: {last_candle_time.strftime('%d.%m.%Y %H:%M')}")
                    if time_diff > timedelta(minutes=30):
                        print(f"⚠️ Внимание: Данные устарели на {time_diff.seconds // 60} минут")
                        return [], [], []
                    else:
                        print("✅ Данные актуальны")
                    print(f"Последняя цена в API: {prices[-1]:.2f} ₽")
                return times, prices, volumes
        except RequestError as e:
            print(f"Ошибка при получении данных: {e}")
            return [], [], []
        except Exception as e:
            print(f"Непредвиденная ошибка при получении данных: {e}")
            return [], [], []

    def calculate_technical_indicators(self, prices, volumes):
        df = pd.DataFrame({'close': prices})
        df['volume'] = volumes
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1 / 14, adjust=False).mean()
        loss = loss.replace(0, np.finfo(float).eps)
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        gain_fast = delta.where(delta > 0, 0).ewm(alpha=1 / 5, adjust=False).mean()
        loss_fast = (-delta.where(delta < 0, 0)).ewm(alpha=1 / 5, adjust=False).mean()
        loss_fast = loss_fast.replace(0, np.finfo(float).eps)
        rs_fast = gain_fast / loss_fast
        df['rsi_fast'] = 100 - (100 / (1 + rs_fast))
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp1 - exp2
        df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['signal']
        df['macd_div'] = df['macd'].diff()
        df['sma'] = df['close'].rolling(window=20).mean()
        df['std'] = df['close'].rolling(window=20).std()
        volatility_factor = df['close'].pct_change().rolling(window=20).std() * 100
        df['bb_width_factor'] = volatility_factor.apply(lambda x: min(3, max(1.5, 2 + x / 10)))
        df['upper_band'] = df['sma'] + (df['std'] * df['bb_width_factor'])
        df['lower_band'] = df['sma'] - (df['std'] * df['bb_width_factor'])
        df['bb_width'] = (df['upper_band'] - df['lower_band']) / df['sma'] * 100
        df['percent_b'] = (df['close'] - df['lower_band']) / (df['upper_band'] - df['lower_band'])
        df['volume_sma'] = df['volume'].rolling(window=5).mean()
        df['volume_sma_long'] = df['volume'].rolling(window=20).mean()
        df['volume_change'] = df['volume'].pct_change() * 100
        df['volume_oscillator'] = (df['volume_sma'] / df['volume_sma_long'] - 1) * 100
        df['hl_range'] = df['close'].diff().abs()
        df['ad_factor'] = np.where(df['hl_range'] > 0, df['close'].diff() / df['hl_range'], 0)
        df['ad_line'] = (df['ad_factor'] * df['volume']).cumsum()
        df['price_ma_5'] = df['close'].rolling(window=5).mean()
        df['price_ma_10'] = df['close'].rolling(window=10).mean()
        df['price_ma_20'] = df['close'].rolling(window=20).mean()
        df['price_ma_50'] = df['close'].rolling(window=min(50, len(df) - 1)).mean()
        df['ma_convergence'] = (df['price_ma_5'] / df['price_ma_20'] - 1) * 100
        df['volatility_short'] = df['close'].pct_change().rolling(window=5).std() * np.sqrt(252)
        df['volatility'] = df['close'].pct_change().rolling(window=20).std() * np.sqrt(252)
        df['high'] = df['close'].rolling(2).max()
        df['low'] = df['close'].rolling(2).min()
        df['tr1'] = df['high'] - df['low']
        df['tr2'] = abs(df['high'] - df['close'].shift())
        df['tr3'] = abs(df['low'] - df['close'].shift())
        df['true_range'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
        df['atr'] = df['true_range'].rolling(window=14).mean()
        df['momentum'] = df['close'].pct_change(periods=10) * 100
        df['roc_5'] = df['close'].pct_change(periods=5) * 100
        df['roc_10'] = df['close'].pct_change(periods=10) * 100
        df['tenkan_sen'] = (df['close'].rolling(window=9).max() + df['close'].rolling(window=9).min()) / 2
        df['kijun_sen'] = (df['close'].rolling(window=26).max() + df['close'].rolling(window=26).min()) / 2
        low_min = df['close'].rolling(window=14).min()
        high_max = df['close'].rolling(window=14).max()
        df['stoch_k'] = 100 * (df['close'] - low_min) / (high_max - low_min)
        df['stoch_d'] = df['stoch_k'].rolling(window=3).mean()
        return df

    def analyze_market_state(self, df):
        market_state = {
            'accumulation': False,
            'distribution': False,
            'bullish': False,
            'bearish': False,
            'correction': False,
            'correction_depth': 0,
            'pullback_opportunity': False,
            'oversold': False,
            'overbought': False,
            'smart_money_buying': False,
            'smart_money_selling': False,
            'retail_buying': False,
            'retail_selling': False,
            'trend_strength': 0,
            'explanation': [],
            'false_breakout': False,
            'false_breakdown': False,
            'potential_reversal': False,
            'whipsaw': False,
            'volatile_consolidation': False,
            'rapid_reversal_risk': 0,
            'false_signal_probability': 0
        }
        if len(df) < 20:
            market_state['explanation'].append("Недостаточно данных для анализа")
            return market_state
        long_term_trend = "neutral"
        if 'price_ma_5' in df.columns and 'price_ma_20' in df.columns and 'price_ma_50' in df.columns:
            ma5_slope = (df['price_ma_5'].iloc[-1] - df['price_ma_5'].iloc[-5]) / df['price_ma_5'].iloc[-5] * 100
            ma20_slope = (df['price_ma_20'].iloc[-1] - df['price_ma_20'].iloc[-5]) / df['price_ma_20'].iloc[-5] * 100
            bullish_alignment = (df['price_ma_5'].iloc[-1] > df['price_ma_20'].iloc[-1] > df['price_ma_50'].iloc[-1])
            bearish_alignment = (df['price_ma_5'].iloc[-1] < df['price_ma_20'].iloc[-1] < df['price_ma_50'].iloc[-1])
            if bullish_alignment and ma5_slope > 0 and ma20_slope > 0:
                long_term_trend = "bullish"
                market_state['bullish'] = True
                market_state['explanation'].append("Бычий тренд: MA5 > MA20 > MA50 с положительными наклонами")
                market_state['trend_strength'] = min(100, int(50 + ma5_slope * 5))
            elif bearish_alignment and ma5_slope < 0 and ma20_slope < 0:
                long_term_trend = "bearish"
                market_state['bearish'] = True
                market_state['explanation'].append("Медвежий тренд: MA5 < MA20 < MA50 с отрицательными наклонами")
                market_state['trend_strength'] = min(100, int(50 - ma5_slope * 5))
            elif df['price_ma_5'].iloc[-1] > df['price_ma_20'].iloc[-1]:
                market_state['bullish'] = True
                market_state['explanation'].append("Умеренный бычий тренд: MA5 > MA20")
                market_state['trend_strength'] = min(100, int(40 + ma5_slope * 3))
            else:
                market_state['bearish'] = True
                market_state['explanation'].append("Умеренный медвежий тренд: MA5 < MA20")
                market_state['trend_strength'] = min(100, int(40 - ma5_slope * 3))
        market_state['correction'] = False
        market_state['correction_depth'] = 0
        if 'rsi' in df.columns:
            last_rsi = df['rsi'].iloc[-1]
            if last_rsi < 30:
                market_state['oversold'] = True
                market_state['explanation'].append(f"Перепроданность: RSI = {last_rsi:.2f}")
                if market_state['bullish']:
                    market_state['pullback_opportunity'] = True
                    market_state['explanation'].append("Возможность покупки: перепроданность в бычьем тренде")
            elif last_rsi > 70:
                market_state['overbought'] = True
                market_state['explanation'].append(f"Перекупленность: RSI = {last_rsi:.2f}")
                if market_state['bearish']:
                    market_state['explanation'].append("Возможность продажи: перекупленность в медвежьем тренде")
        if 'macd' in df.columns and 'close' in df.columns and len(df) > 20:
            recent_macd = df['macd'].iloc[-10:].values
            recent_prices = df['close'].iloc[-10:].values
            price_max_idx = np.argmax(recent_prices)
            price_min_idx = np.argmin(recent_prices)
            macd_max_idx = np.argmax(recent_macd)
            macd_min_idx = np.argmin(recent_macd)
            if price_max_idx > macd_max_idx and abs(price_max_idx - macd_max_idx) > 2:
                market_state['explanation'].append("Обнаружена медвежья дивергенция: цена растет, MACD падает (сигнал к возможному развороту вниз)")
                if recent_prices[price_max_idx] > recent_prices[macd_max_idx] * 1.02:
                    market_state['potential_reversal'] = True
                    market_state['explanation'].append("Существенная медвежья дивергенция: возможно скорое окончание восходящего тренда")
                    market_state['rapid_reversal_risk'] = min(100, market_state.get('rapid_reversal_risk', 0) + 40)
                    if market_state.get('overbought', False):
                        market_state['false_breakout'] = True
                        market_state['explanation'].append("Перекупленность + медвежья дивергенция: высокая вероятность ложного движения вверх")
            if price_min_idx > macd_min_idx and abs(price_min_idx - macd_min_idx) > 2:
                market_state['explanation'].append("Обнаружена бычья дивергенция: цена падает, MACD растет (сигнал к возможному развороту вверх)")
                if recent_prices[price_min_idx] < recent_prices[macd_min_idx] * 0.98:
                    market_state['potential_reversal'] = True
                    market_state['explanation'].append("Существенная бычья дивергенция: возможно скорое окончание нисходящего тренда")
                    market_state['rapid_reversal_risk'] = min(100, market_state.get('rapid_reversal_risk', 0) + 40)
                    if market_state.get('oversold', False):
                        market_state['false_breakdown'] = True
                        market_state['explanation'].append("Перепроданность + бычья дивергенция: высокая вероятность ложного движения вниз")
            if len(recent_macd) >= 5:
                macd_slope_early = recent_macd[1] - recent_macd[0]
                macd_slope_late = recent_macd[-1] - recent_macd[-2]
                if macd_slope_early > 0 and macd_slope_late > 0 and macd_slope_late < macd_slope_early * 0.5:
                    market_state['explanation'].append("Замедление роста MACD: возможное ослабление восходящего импульса")
                    if market_state.get('bullish', False):
                        market_state['false_signal_probability'] = min(75, market_state.get('false_signal_probability', 0) + 30)
                if macd_slope_early < 0 and macd_slope_late < 0 and abs(macd_slope_late) < abs(macd_slope_early) * 0.5:
                    market_state['explanation'].append("Замедление падения MACD: возможное ослабление нисходящего импульса")
                    if market_state.get('bearish', False):
                        market_state['false_signal_probability'] = min(75, market_state.get('false_signal_probability', 0) + 30)
            if 'signal' in df.columns and len(df) > 20:
                recent_signal = df['signal'].iloc[-10:].values
                signal_max_idx = np.argmax(recent_signal)
                signal_min_idx = np.argmin(recent_signal)
                if (price_max_idx > macd_max_idx and price_max_idx > signal_max_idx and abs(price_max_idx - signal_max_idx) > 3):
                    market_state['explanation'].append("Подтвержденная медвежья дивергенция (цена, MACD, сигнальная линия): высокая вероятность разворота вниз")
                    market_state['potential_reversal'] = True
                    market_state['rapid_reversal_risk'] = min(100, market_state.get('rapid_reversal_risk', 0) + 60)
                if (price_min_idx > macd_min_idx and price_min_idx > signal_min_idx and abs(price_min_idx - signal_min_idx) > 3):
                    market_state['explanation'].append("Подтвержденная бычья дивергенция (цена, MACD, сигнальная линия): высокая вероятность разворота вверх")
                    market_state['potential_reversal'] = True
                    market_state['rapid_reversal_risk'] = min(100, market_state.get('rapid_reversal_risk', 0) + 60)
            if 'volume' in df.columns and 'volume_sma' in df.columns and len(df) > 20:
                recent_df = df.iloc[-10:]
                volume_trend = (recent_df['volume'].iloc[-5:].mean() / recent_df['volume'].iloc[:5].mean() - 1) * 100
                if volume_trend > 20:
                    market_state['explanation'].append(f"Растущий объем торгов: +{volume_trend:.1f}%")
                elif volume_trend < -20:
                    market_state['explanation'].append(f"Снижающийся объем торгов: {volume_trend:.1f}%")
                high_volume_bars = recent_df[recent_df['volume'] > 1.5 * recent_df['volume_sma']]
                if not high_volume_bars.empty:
                    price_direction = high_volume_bars['close'].diff().sum()
                    if len(high_volume_bars) >= 2:
                        first_bars = high_volume_bars.iloc[:len(high_volume_bars) // 2]
                        last_bars = high_volume_bars.iloc[len(high_volume_bars) // 2:]
                        first_direction = first_bars['close'].diff().sum()
                        last_direction = last_bars['close'].diff().sum()
                        if (first_direction > 0 and last_direction < 0 and abs(last_direction) > abs(first_direction) * 0.7):
                            market_state['false_breakout'] = True
                            market_state['explanation'].append("Обнаружен ложный пробой вверх: рост и резкий разворот вниз на высоком объеме")
                            market_state['false_signal_probability'] = min(85, 50 + (abs(last_direction / first_direction) * 30))
                            market_state['rapid_reversal_risk'] = 75
                        elif (first_direction < 0 and last_direction > 0 and abs(last_direction) > abs(first_direction) * 0.7):
                            market_state['false_breakdown'] = True
                            market_state['explanation'].append("Обнаружен ложный пробой вниз: падение и резкий разворот вверх на высоком объеме")
                            market_state['false_signal_probability'] = min(85, 50 + (abs(last_direction / first_direction) * 30))
                            market_state['rapid_reversal_risk'] = 75
                        if len(high_volume_bars) >= 3:
                            directions = np.sign(high_volume_bars['close'].diff().dropna().values)
                            direction_changes = np.diff(directions, prepend=directions[0])
                            if np.count_nonzero(direction_changes) >= len(directions) * 0.6:
                                market_state['whipsaw'] = True
                                market_state['explanation'].append("Рынок в состоянии 'пилы': резкие разнонаправленные движения")
                                market_state['volatile_consolidation'] = True
                    if price_direction > 0:
                        market_state['smart_money_buying'] = True
                        if market_state['bullish']:
                            market_state['explanation'].append("Подтверждение бычьего тренда: высокий объем с ростом цены")
                        else:
                            market_state['explanation'].append("Возможная смена тренда на бычий: высокий объем с ростом цены")
                    elif price_direction < 0:
                        market_state['smart_money_selling'] = True
                        if market_state['bearish']:
                            market_state['explanation'].append("Подтверждение медвежьего тренда: высокий объем с падением цены")
                        else:
                            market_state['explanation'].append("Возможная смена тренда на медвежий: высокий объем с падением цены")
                low_volume_bars = recent_df[recent_df['volume'] < 0.7 * recent_df['volume_sma']]
                if not low_volume_bars.empty:
                    price_direction = low_volume_bars['close'].diff().sum()
                    if price_direction > 0 and market_state['bearish']:
                        market_state['explanation'].append("Коррекция в медвежьем тренде: рост цены на низком объеме")
                    elif price_direction < 0 and market_state['bullish']:
                        market_state['explanation'].append("Коррекция в бычьем тренде: падение цены на низком объеме")
                        market_state['pullback_opportunity'] = True
        if len(df) > 30:
            if 'volatility' in df.columns and 'volatility_short' in df.columns:
                current_volatility = df['volatility_short'].iloc[-1]
                previous_volatility = df['volatility_short'].iloc[-10]
                if current_volatility < previous_volatility * 0.7:
                    market_state['explanation'].append("Снижение волатильности: возможная консолидация перед новым движением")
        return market_state

    def predict_multiple_intervals(self, times, prices, volumes):
        if len(prices) < 20:
            return None, None, None, None, None
        df = self.calculate_technical_indicators(prices, volumes)
        df['price_diff'] = df['close'].diff()
        df = df.dropna()
        if len(df) < 20:
            return None, None, None, None, None
        market_state = self.analyze_market_state(df)
        self.last_market_state = market_state
        self.ma5 = df['price_ma_5'].iloc[-1]
        self.ma20 = df['price_ma_20'].iloc[-1]
        feature_columns = ['rsi', 'macd', 'signal', 'volume', 'volume_sma', 'price_ma_5', 'price_ma_20', 'volatility', 'upper_band', 'lower_band', 'price_diff']
        available_features = [col for col in feature_columns if col in df.columns]
        is_uptrend = df['price_ma_5'].iloc[-1] > df['price_ma_20'].iloc[-1]
        X = df[available_features].values
        y = df['close'].values
        scaler_X = StandardScaler()
        X_scaled = scaler_X.fit_transform(X)
        scaler_y = StandardScaler()
        y_scaled = scaler_y.fit_transform(y.reshape(-1, 1)).flatten()
        predictions = {}
        intervals = ["15", "30", "60"]
        trend_coefficient = 1.0
        if len(prices) > 10:
            weights_arr = np.exp(np.linspace(0, 1, 10))
            weights_arr = weights_arr / weights_arr.sum()
            recent_prices = prices[-10:]
            weighted_trend = np.polyfit(range(10), recent_prices, 1, w=weights_arr)[0]
            if weighted_trend > 0:
                trend_coefficient = 1.2 + min(0.3, abs(weighted_trend) * 5)
            else:
                trend_coefficient = 0.8 - min(0.2, abs(weighted_trend) * 3)
        market_volatility = np.std(df['close'].pct_change().dropna()) * 100
        volatility_factor = min(1.5, max(0.5, 1 + market_volatility / 10))
        for interval in intervals:
            window = int(int(interval) / 5)
            if len(X_scaled) > window:
                X_train = X_scaled[:-window]
                y_train = y_scaled[window:]
                model_lr = LinearRegression()
                model_lr.fit(X_train, y_train)
                last_features = X_scaled[-1:]
                pred_lr = model_lr.predict(last_features)[0]
                from sklearn.preprocessing import PolynomialFeatures
                from sklearn.pipeline import Pipeline
                model_poly = Pipeline([('poly', PolynomialFeatures(degree=2)), ('linear', LinearRegression())])
                recent_window = min(30, len(X_train))
                model_poly.fit(X_train[-recent_window:], y_train[-recent_window:])
                pred_poly = model_poly.predict(last_features)[0]
                from sklearn.ensemble import GradientBoostingRegressor
                model_gb = GradientBoostingRegressor(n_estimators=50, learning_rate=0.1, max_depth=3, random_state=42)
                try:
                    model_gb.fit(X_train, y_train)
                    pred_gb = model_gb.predict(last_features)[0]
                except Exception:
                    pred_gb = pred_lr
                if market_volatility > 1.5:
                    weights = ENSEMBLE_WEIGHTS_HIGH_VOL
                else:
                    weights = ENSEMBLE_WEIGHTS_LOW_VOL
                pred_ensemble = (weights[0] * pred_lr + weights[1] * pred_poly + weights[2] * pred_gb)
                pred_price = scaler_y.inverse_transform([[pred_ensemble]])[0][0]
                trend_adjust = 0.0
                if is_uptrend:
                    trend_adjust = prices[-1] * 0.003 * trend_coefficient * (int(interval) / 15) * volatility_factor
                else:
                    trend_adjust = -prices[-1] * 0.002 * (2 - trend_coefficient) * (int(interval) / 15) * volatility_factor
                pred_price += trend_adjust
                future_times = [times[-1] + timedelta(minutes=5 * i) for i in range(1, window + 1)]
                confidence_interval = market_volatility * 0.1 * np.sqrt(int(interval) / 15)
                predictions[interval] = {
                    'times': future_times,
                    'price': pred_price,
                    'change': ((pred_price - prices[-1]) / prices[-1]) * 100,
                    'confidence': confidence_interval
                }
        return predictions, df['price_ma_5'].iloc[-1], df['price_ma_20'].iloc[-1], df['volatility'].iloc[-1], market_state

    def plot_prediction(self, times, prices, predictions):
        plt.figure(figsize=(15, 8))
        plt.style.use('seaborn-v0_8-darkgrid')
        has_market_state = hasattr(self, 'last_market_state')
        market_state = getattr(self, 'last_market_state', {})
        is_bullish = market_state.get('bullish', False) if has_market_state else False
        is_bearish = market_state.get('bearish', False) if has_market_state else False
        is_correction = market_state.get('correction', False) if has_market_state else False
        trend_strength = market_state.get('trend_strength', 50) if has_market_state else 50
        plt.plot(times, prices, color='#2E86C1', label='Исторические цены', linewidth=2)
        if hasattr(self, 'ma5') and hasattr(self, 'ma20'):
            ma5_values = [self.ma5] * len(times)
            ma20_values = [self.ma20] * len(times)
            plt.plot(times, ma5_values, color='#F39C12', label='MA5', linewidth=1.5, linestyle='-', alpha=0.7)
            plt.plot(times, ma20_values, color='#8E44AD', label='MA20', linewidth=1.5, linestyle='-', alpha=0.7)
        colors = {'15': '#E74C3C', '30': '#2ECC71', '60': '#9B59B6'}
        for interval, data in predictions.items():
            if isinstance(data, dict) and 'price' in data:
                n_points = int(int(interval) / 5)
                future_times = [times[-1] + timedelta(minutes=5 * i) for i in range(1, n_points + 1)]
                predicted_prices = [data['price']] * len(future_times)
                plt.plot(future_times, predicted_prices, color=colors[interval], label=f'Прогноз {interval}м', linewidth=2, linestyle='--')
                if 'confidence' in data:
                    confidence = data['confidence']
                    upper_bound = [p + (p * confidence / 100) for p in predicted_prices]
                    lower_bound = [p - (p * confidence / 100) for p in predicted_prices]
                    plt.fill_between(future_times, lower_bound, upper_bound, color=colors[interval], alpha=0.2)
        if len(prices) >= 10:
            window_size = 5
            for i in range(window_size, len(prices) - window_size):
                is_local_max = True
                is_local_min = True
                for j in range(1, window_size + 1):
                    if prices[i] <= prices[i - j] or prices[i] <= prices[i + j]:
                        is_local_max = False
                    if prices[i] >= prices[i - j] or prices[i] >= prices[i + j]:
                        is_local_min = False
                if is_local_max:
                    plt.scatter([times[i]], [prices[i]], color='red', s=80, marker='^', zorder=5)
                    if i > 0 and (prices[i] / prices[i - 1] - 1) * 100 > 0.5:
                        plt.annotate('Пик', xy=(times[i], prices[i]), xytext=(0, 10), textcoords='offset points', fontsize=8, ha='center', bbox=dict(boxstyle="round,pad=0.1", facecolor='white', alpha=0.7))
                if is_local_min:
                    plt.scatter([times[i]], [prices[i]], color='green', s=80, marker='v', zorder=5)
                    if i > 0 and (1 - prices[i] / prices[i - 1]) * 100 > 0.5:
                        plt.annotate('Мин', xy=(times[i], prices[i]), xytext=(0, -15), textcoords='offset points', fontsize=8, ha='center', bbox=dict(boxstyle="round,pad=0.1", facecolor='white', alpha=0.7))
        info_text = []
        if is_bullish:
            trend_text = f"⬆️ БЫЧИЙ ТРЕНД (сила: {trend_strength}%)"
            info_text.append(trend_text)
        elif is_bearish:
            trend_text = f"⬇️ МЕДВЕЖИЙ ТРЕНД (сила: {trend_strength}%)"
            info_text.append(trend_text)
        else:
            info_text.append("↔️ БОКОВОЙ ТРЕНД")
        if market_state.get('oversold', False):
            info_text.append("⚠️ Перепроданность")
        if market_state.get('overbought', False):
            info_text.append("⚠️ Перекупленность")
        if market_state.get('pullback_opportunity', False):
            info_text.append("✅ Возможность входа на откате")
        for i, text in enumerate(info_text):
            plt.annotate(text, xy=(0.02, 0.95 - i * 0.05), xycoords='axes fraction', fontsize=10, bbox=dict(boxstyle="round,pad=0.2", facecolor='white', alpha=0.8))
        plt.title(f'Прогноз цены акции {self.ticker} с анализом трендов и коррекций', fontsize=16, fontweight='bold', pad=20)
        plt.xlabel('Время', fontsize=12)
        plt.ylabel('Цена (₽ за акцию)', fontsize=12)
        plt.grid(True, alpha=0.3)
        if prices:
            last_price = prices[-1]
            last_time = times[-1]
            plt.scatter([last_time], [last_price], color='blue', s=100, zorder=6)
            plt.annotate(f'{last_price:.2f} ₽', xy=(last_time, last_price), xytext=(10, 0), textcoords='offset points', fontsize=10, fontweight='bold', bbox=dict(boxstyle="round,pad=0.2", facecolor='white', alpha=0.7))
        if hasattr(self, 'last_recommendation'):
            rec_text = self.last_recommendation.split(" - ")[0]
            rec_color = "#2ECC71" if "ПОКУПАТЬ" in rec_text else "#E74C3C"
            plt.annotate(rec_text, xy=(0.98, 0.05), xycoords='axes fraction', fontsize=12, fontweight='bold', color=rec_color, ha='right', bbox=dict(boxstyle="round,pad=0.3", facecolor='white', alpha=0.9))
        legend = plt.legend(loc='upper left', frameon=True, fancybox=True, shadow=True)
        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.savefig('static/stock_prediction.png', dpi=300, bbox_inches='tight')
        plt.close()

import os
import json
from datetime import datetime, timedelta

PREDICTION_HISTORY_DIR = 'data/predictions'
if not os.path.exists(PREDICTION_HISTORY_DIR):
    os.makedirs(PREDICTION_HISTORY_DIR)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/analyze")
async def analyze(ticker: str = Form(...), use_meta_learning: bool = Form(False)):
    if not ticker:
        return JSONResponse({"error": "Пожалуйста, введите тикер акции"})
    predictor = StockPredictor()
    if not predictor.set_ticker(ticker):
        return JSONResponse({"error": f"Тикер {ticker} не найден"})
    print(f"Анализ акции {ticker}...")
    times, prices, volumes = predictor.collect_data()
    if not prices or len(prices) < 20:
        return JSONResponse({"error": "Недостаточно данных для анализа"})
    predictions, ma5, ma20, volatility, market_state = predictor.predict_multiple_intervals(times, prices, volumes)
    predictor.last_volatility = volatility
    price_change = ((prices[-1] - prices[0]) / prices[0]) * 100
    momentum = predictor.calculate_momentum(prices)
    df = predictor.calculate_technical_indicators(prices, volumes)
    last_rsi = df['rsi'].iloc[-1]
    last_macd = df['macd'].iloc[-1]
    last_signal = df['signal'].iloc[-1]
    recommendation, reasons, entry_exit_prices = predictor.get_recommendation(last_rsi, last_macd, last_signal, price_change, momentum, prices[-1])
    predictor.last_recommendation = recommendation
    prediction_data = {}
    for interval, data in predictions.items():
        prediction_data[interval] = {'price': data['price'], 'change': data['change']}
    prediction_data['current_price'] = prices[-1]
    prediction_data['volatility'] = volatility
    meta_learning_details = None
    if use_meta_learning:
        try:
            from prediction_analytics import PredictionAnalytics
            analytics = PredictionAnalytics()
            corrected_predictions, meta_learning_details = analytics.apply_meta_learning_corrections(ticker, prediction_data)
            if meta_learning_details and meta_learning_details.get('applied', False):
                prediction_data = corrected_predictions
        except Exception as e:
            print(f"Ошибка при применении метаобучения: {e}")
            meta_learning_details = {"error": str(e), "applied": False}
    predictor.plot_prediction(times, prices, prediction_data)
    market_state_data = {
        'bullish': market_state.get('bullish', False),
        'bearish': market_state.get('bearish', False),
        'correction': market_state.get('correction', False),
        'trend_strength': market_state.get('trend_strength', 0),
        'correction_depth': market_state.get('correction_depth', 0),
        'explanation': market_state.get('explanation', [])
    }
    result = {
        'ticker': ticker,
        'current_price': prices[-1],
        'ma5': ma5,
        'ma20': ma20,
        'trend': 'ВОСХОДЯЩИЙ' if ma5 > ma20 else 'НИСХОДЯЩИЙ',
        'price_change': price_change,
        'volatility': volatility,
        'momentum': momentum,
        'rsi': last_rsi,
        'macd': last_macd,
        'signal_line': last_signal,
        'recommendation': recommendation,
        'reasons': reasons,
        'entry_exit_prices': entry_exit_prices,
        'predictions': prediction_data,
        'market_state': market_state_data,
        'chart_path': '/static/stock_prediction.png'
    }
    result['confidence_level'] = calculate_recommendation_confidence(reasons, market_state_data, price_change, volatility)
    if meta_learning_details:
        result['meta_learning'] = meta_learning_details
    save_prediction_history(ticker, prices[-1], prediction_data)
    return result

@app.post("/auto_update")
async def auto_update(ticker: str = Form(...)):
    predictor = StockPredictor()
    if not predictor.set_ticker(ticker):
        return JSONResponse({"error": f"Тикер {ticker} не найден"})
    times, prices, volumes = predictor.collect_data()
    if not prices or len(prices) < 20:
        return JSONResponse({"error": "Недостаточно данных для анализа"})
    predictions, ma5, ma20, volatility, market_state = predictor.predict_multiple_intervals(times, prices, volumes)
    predictor.last_volatility = volatility
    price_change = ((prices[-1] - prices[0]) / prices[0]) * 100
    momentum = predictor.calculate_momentum(prices)
    df = predictor.calculate_technical_indicators(prices, volumes)
    last_rsi = df['rsi'].iloc[-1]
    last_macd = df['macd'].iloc[-1]
    last_signal = df['signal'].iloc[-1]
    recommendation, reasons, entry_exit_prices = predictor.get_recommendation(last_rsi, last_macd, last_signal, price_change, momentum, prices[-1])
    predictor.last_recommendation = recommendation
    predictor.plot_prediction(times, prices, predictions)
    prediction_data = {}
    for interval, data in predictions.items():
        prediction_data[interval] = {'price': data['price'], 'change': data['change']}
    save_prediction_history(ticker, prices[-1], prediction_data)
    return {
        'ticker': ticker,
        'current_price': prices[-1],
        'rsi': last_rsi,
        'macd': last_macd,
        'signal_line': last_signal,
        'price_change': price_change,
        'momentum': momentum,
        'recommendation': recommendation,
        'predictions': prediction_data,
        'chart_path': f'/static/stock_prediction.png?t={datetime.now().timestamp()}'
    }

from prediction_analytics import PredictionAnalytics

@app.get("/prediction_accuracy/{ticker}")
async def prediction_accuracy(ticker: str):
    analytics = PredictionAnalytics()
    try:
        accuracy_data = analytics.evaluate_prediction_quality(ticker)
        if isinstance(accuracy_data, dict) and "error" in accuracy_data:
            accuracy_data = analytics.calculate_advanced_metrics(ticker)
    except Exception as e:
        print(f"Ошибка при использовании нового метода оценки: {e}")
        accuracy_data = analytics.calculate_advanced_metrics(ticker)
    if not accuracy_data:
        return JSONResponse({"error": "Недостаточно данных для анализа точности"})
    return accuracy_data

@app.get("/advanced_analytics/{ticker}")
async def advanced_analytics(ticker: str):
    predictor = StockPredictor()
    if not predictor.set_ticker(ticker):
        return JSONResponse({"error": f"Тикер {ticker} не найден"})
    times, prices, volumes = predictor.collect_data()
    if not prices or len(prices) < 40:
        return JSONResponse({"error": "Недостаточно данных для расширенной аналитики (требуется минимум 40 точек)"})
    df = predictor.calculate_technical_indicators(prices, volumes)
    df = df.dropna()
    feature_columns = ['rsi', 'macd', 'signal', 'price_ma_5', 'price_ma_20', 'volatility', 'momentum', 'roc_5', 'roc_10', 'stoch_k']
    available_features = [col for col in feature_columns if col in df.columns]
    features = df[available_features].values
    target = df['close'].values
    analytics = PredictionAnalytics()
    cv_results = analytics.perform_cross_validation(ticker, target, features)
    hyperparameter_results = analytics.get_optimal_hyperparameters(ticker, target, features)
    if not cv_results:
        return JSONResponse({"error": "Не удалось выполнить кросс-валидацию"})
    try:
        advanced_models_result = analytics.combine_advanced_models(ticker, prices)
        return {
            "cross_validation": cv_results,
            "hyperparameters": hyperparameter_results,
            "advanced_models": advanced_models_result
        }
    except Exception as e:
        return {
            "cross_validation": cv_results,
            "hyperparameters": hyperparameter_results,
            "advanced_models_error": str(e)
        }

def save_prediction_history(ticker, current_price, predictions):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ticker_dir = os.path.join(PREDICTION_HISTORY_DIR, ticker)
    if not os.path.exists(ticker_dir):
        os.makedirs(ticker_dir)
    prediction_data = {
        'timestamp': datetime.now().isoformat(),
        'current_price': current_price,
        'predictions': predictions
    }
    filename = os.path.join(ticker_dir, f"{timestamp}.json")
    with open(filename, 'w') as f:
        json.dump(prediction_data, f)

def calculate_recommendation_confidence(reasons, market_state, price_change, volatility):
    total_signals = len(reasons)
    bullish_indicators = sum(1 for reason in reasons if "сильный сигнал к покупке" in reason or "умеренный сигнал к покупке" in reason or "хорошая возможность для покупки" in reason or "бычий тренд" in reason)
    bearish_indicators = sum(1 for reason in reasons if "сильный сигнал к продаже" in reason or "умеренный сигнал к продаже" in reason or "возможность для продажи" in reason or "медвежий тренд" in reason)
    neutral_indicators = total_signals - bullish_indicators - bearish_indicators
    base_confidence = (bullish_indicators + 0.5 * neutral_indicators) / max(1, total_signals) * 100
    if volatility > 2.0:
        volatility_factor = 0.7
    elif volatility > 1.5:
        volatility_factor = 0.8
    elif volatility < 0.5:
        volatility_factor = 1.2
    else:
        volatility_factor = 1.0
    if abs(price_change) > 2:
        price_change_factor = 0.9
    else:
        price_change_factor = 1.0
    if bullish_indicators >= 2 and bearish_indicators >= 2:
        contradictory_factor = 0.85
    else:
        contradictory_factor = 1.0
    confidence = base_confidence * volatility_factor * price_change_factor * contradictory_factor
    return min(100, max(0, int(confidence)))

if __name__ == '__main__':
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
