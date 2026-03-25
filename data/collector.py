"""
data/collector.py — Step 1 & 2: Environment Scanner + Signal Gatherer.

Pulls from:
  - Binance  : OHLCV candles (1h + 4h), current price, order book
  - CryptoPanic: Crypto news headlines (free tier, key optional)
  - Alternative.me: Fear & Greed Index (free, no key)
  - Calculated: Technical indicators (RSI, StochRSI, MACD, BB, EMA, ATR, ADX, OBV, VWAP)

No external TA library required — all indicators use pandas + numpy.
"""
import requests
import pandas as pd
import numpy as np
from binance.client import Client as BinanceClient
from loguru import logger
from datetime import datetime, timezone
import config
import constants as C


class DataCollectionError(RuntimeError):
    """Raised when a critical data fetch fails and the cycle cannot continue."""


# ── Technical Indicator Implementations ───────────────────────────────────

def _safe_divide(a, b, default=np.nan):
    """Element-wise safe division; replaces zero/NaN denominators with `default`."""
    if isinstance(b, pd.Series):
        return np.where(b != 0, a / b.replace(0, np.nan), default)
    return a / b if b != 0 else default


def _rsi(close: pd.Series, length: int = C.RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(com=length - 1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=length - 1, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _stoch_rsi(
    close: pd.Series,
    rsi_len: int = C.STOCH_RSI_PERIOD,
    stoch_len: int = C.STOCH_RSI_PERIOD,
    k: int = C.STOCH_K,
    d: int = C.STOCH_D,
):
    """Stochastic RSI — more sensitive entry/exit signal than plain RSI."""
    rsi = _rsi(close, rsi_len)
    min_rsi = rsi.rolling(stoch_len).min()
    max_rsi = rsi.rolling(stoch_len).max()
    denom = (max_rsi - min_rsi).replace(0, np.nan)
    stoch = (rsi - min_rsi) / denom * 100
    k_line = stoch.rolling(k).mean()
    d_line = k_line.rolling(d).mean()
    return k_line, d_line


def _ema(close: pd.Series, length: int) -> pd.Series:
    return close.ewm(span=length, adjust=False).mean()


def _macd(
    close: pd.Series,
    fast: int = C.MACD_FAST,
    slow: int = C.MACD_SLOW,
    signal: int = C.MACD_SIGNAL,
):
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def _bbands(close: pd.Series, length: int = C.BB_PERIOD, std: float = C.BB_STD):
    sma = close.rolling(window=length).mean()
    s = close.rolling(window=length).std()
    return sma + std * s, sma - std * s, sma   # upper, lower, mid


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = C.ATR_PERIOD) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=length - 1, adjust=False).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = C.ADX_PERIOD):
    """
    Average Directional Index — measures trend strength.
    ADX > 25 = strong trend, ADX < 20 = ranging/consolidating.
    Returns (adx, +DI, -DI).
    """
    atr14 = _atr(high, low, close, length)
    up_move = high.diff()
    down_move = -low.diff()
    pos_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    neg_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    smooth_pos = pos_dm.ewm(com=length - 1, adjust=False).mean()
    smooth_neg = neg_dm.ewm(com=length - 1, adjust=False).mean()
    atr_safe = atr14.replace(0, np.nan)
    pos_di = 100 * smooth_pos / atr_safe
    neg_di = 100 * smooth_neg / atr_safe
    di_sum = (pos_di + neg_di).replace(0, np.nan)
    dx = 100 * (pos_di - neg_di).abs() / di_sum
    adx = dx.ewm(com=length - 1, adjust=False).mean()
    return adx, pos_di, neg_di


def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume — confirms price moves with volume."""
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()


def _vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Volume-Weighted Average Price — key institutional reference level."""
    typical = (high + low + close) / 3
    cumvol = volume.cumsum()
    # Guard against zero cumulative volume (e.g. all-zero volume data)
    cumvol_safe = cumvol.replace(0, np.nan)
    return (typical * volume).cumsum() / cumvol_safe


def _williams_r(high: pd.Series, low: pd.Series, close: pd.Series, length: int = C.WILLIAMS_R_PERIOD) -> pd.Series:
    """Williams %R — overbought/oversold. -80 to -100 = oversold, 0 to -20 = overbought."""
    highest_high = high.rolling(length).max()
    lowest_low = low.rolling(length).min()
    denom = (highest_high - lowest_low).replace(0, np.nan)
    return -100 * (highest_high - close) / denom


# ── Main Collector ─────────────────────────────────────────────────────────

class DataCollector:
    def __init__(self):
        self.binance = BinanceClient(
            config.BINANCE_API_KEY,
            config.BINANCE_SECRET_KEY,
            requests_params={"timeout": C.API_TIMEOUT_SECONDS},
        )
        if config.BINANCE_TESTNET:
            self.binance.API_URL = "https://testnet.binance.vision/api"

    # ── Price Data ────────────────────────────────────────────────────────

    def get_ohlcv(self, pair: str, interval: str = "1h", limit: int = 150) -> pd.DataFrame:
        """
        Fetch OHLCV candles from Binance.
        Raises DataCollectionError on failure or if data is empty so the cycle is skipped cleanly.
        """
        from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type(Exception),
            reraise=True
        )
        def _fetch():
            return self.binance.get_klines(symbol=pair, interval=interval, limit=limit)

        try:
            klines = _fetch()
            if not klines:
                raise DataCollectionError(f"Empty OHLCV data returned for {pair}/{interval}")

            df = pd.DataFrame(klines, columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_vol", "num_trades", "taker_base",
                "taker_quote", "ignore"
            ])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col])
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            df.set_index("open_time", inplace=True)
            return df
        except Exception as e:
            logger.error(f"OHLCV fetch failed for {pair}/{interval} after retries: {e}")
            raise DataCollectionError(f"Cannot fetch OHLCV for {pair}/{interval}") from e

    def get_current_price(self, pair: str) -> float:
        """Returns 0.0 on failure (non-critical — indicators already capture price)."""
        from tenacity import retry, stop_after_attempt, wait_exponential
        
        @retry(
            stop=stop_after_attempt(2),
            wait=wait_exponential(multiplier=1, min=1, max=5),
            reraise=False
        )
        def _fetch():
            ticker = self.binance.get_symbol_ticker(symbol=pair)
            return float(ticker["price"])

        try:
            price = _fetch()
            return price if price is not None else 0.0
        except Exception as e:
            logger.error(f"Price fetch failed for {pair}: {e}")
            return 0.0

    def get_order_book_imbalance(self, pair: str) -> float | None:
        """
        Order book bid/ask imbalance. Range: -1.0 to +1.0
        Returns None when data is unavailable (distinct from 0.0 = balanced).
        """
        try:
            book = self.binance.get_order_book(symbol=pair, limit=C.ORDER_BOOK_DEPTH)
            bid_vol = sum(float(b[1]) for b in book["bids"])
            ask_vol = sum(float(a[1]) for a in book["asks"])
            total = bid_vol + ask_vol
            if total == 0:
                return None
            return round((bid_vol - ask_vol) / total, 4)
        except Exception as e:
            logger.error(f"Order book fetch failed for {pair}: {e}")
            return None

    def get_usdt_balance(self) -> float:
        try:
            if config.TRADE_MODE == "futures":
                balances = self.binance.futures_account_balance()
                for b in balances:
                    if b["asset"] == "USDT":
                        return float(b["availableBalance"])
                return 0.0
            else:
                account = self.binance.get_account()
                for asset in account["balances"]:
                    if asset["asset"] == "USDT":
                        return float(asset["free"])
                return 0.0
        except Exception as e:
            logger.error(f"Balance fetch failed: {e}")
            return 0.0

    def get_24h_stats(self, pair: str) -> dict:
        """24h high, low, volume, and price change % from Binance ticker."""
        try:
            t = self.binance.get_ticker(symbol=pair)
            return {
                "high_24h":              float(t["highPrice"]),
                "low_24h":               float(t["lowPrice"]),
                "volume_24h":            float(t["volume"]),
                "quote_volume_24h":      float(t["quoteVolume"]),
                "price_change_pct_24h":  float(t["priceChangePercent"]),
                "weighted_avg_price":    float(t["weightedAvgPrice"]),
            }
        except Exception as e:
            logger.error(f"24h stats fetch failed for {pair}: {e}")
            return {}

    # ── Technical Indicators ──────────────────────────────────────────────

    def compute_indicators(self, df: pd.DataFrame) -> dict:
        """
        Computes all technical indicators and returns a clean signal dict.
        Requires >= MIN_CANDLES_FOR_INDICATORS candles for reliable warmup.
        """
        if df.empty or len(df) < C.MIN_CANDLES_FOR_INDICATORS:
            logger.warning(
                f"Not enough candles to compute indicators "
                f"(have {len(df)}, need >= {C.MIN_CANDLES_FOR_INDICATORS})"
            )
            return {}

        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        vol    = df["volume"]
        price  = float(close.iloc[-1])
        signals: dict = {}

        # ── RSI ───────────────────────────────────────────────────────────
        rsi = _rsi(close)
        signals["rsi"] = round(float(rsi.iloc[-1]), 2)
        signals["rsi_zone"] = (
            "oversold"   if signals["rsi"] < 30 else
            "overbought" if signals["rsi"] > 70 else
            "neutral"
        )

        # ── Stochastic RSI ────────────────────────────────────────────────
        stoch_k, stoch_d = _stoch_rsi(close)
        signals["stoch_rsi_k"] = round(float(stoch_k.iloc[-1]), 2)
        signals["stoch_rsi_d"] = round(float(stoch_d.iloc[-1]), 2)
        signals["stoch_rsi_signal"] = (
            "oversold"   if signals["stoch_rsi_k"] < 20 else
            "overbought" if signals["stoch_rsi_k"] > 80 else
            "neutral"
        )
        signals["stoch_cross"] = "bullish" if signals["stoch_rsi_k"] > signals["stoch_rsi_d"] else "bearish"

        # ── MACD ──────────────────────────────────────────────────────────
        macd_line, signal_line, macd_hist = _macd(close)
        signals["macd"]        = round(float(macd_line.iloc[-1]), 6)
        signals["macd_signal"] = round(float(signal_line.iloc[-1]), 6)
        signals["macd_hist"]   = round(float(macd_hist.iloc[-1]), 6)
        signals["macd_cross"]  = "bullish" if signals["macd"] > signals["macd_signal"] else "bearish"
        hist_prev = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else signals["macd_hist"]
        signals["macd_momentum"] = "increasing" if abs(signals["macd_hist"]) > abs(hist_prev) else "decreasing"

        # ── Bollinger Bands ───────────────────────────────────────────────
        bb_upper_s, bb_lower_s, bb_mid_s = _bbands(close)
        bb_upper = float(bb_upper_s.iloc[-1])
        bb_lower = float(bb_lower_s.iloc[-1])
        bb_mid   = float(bb_mid_s.iloc[-1])
        signals["bb_upper"] = round(bb_upper, 4)
        signals["bb_lower"] = round(bb_lower, 4)
        signals["bb_mid"]   = round(bb_mid, 4)
        bb_range = bb_upper - bb_lower
        signals["bb_width"]    = round(bb_range / bb_mid, 4) if bb_mid > 0 else 0.0
        signals["bb_position"] = round((price - bb_lower) / bb_range, 3) if bb_range > 0 else 0.5
        signals["bb_squeeze"]  = signals["bb_width"] < C.BB_SQUEEZE_THRESHOLD

        # ── EMA Trend ─────────────────────────────────────────────────────
        ema20  = _ema(close, C.EMA_SHORT)
        ema50  = _ema(close, C.EMA_MID)
        ema200 = _ema(close, C.EMA_LONG) if len(close) >= C.EMA_LONG else None
        e20 = float(ema20.iloc[-1])
        e50 = float(ema50.iloc[-1])
        signals["ema20"]       = round(e20, 4)
        signals["ema50"]       = round(e50, 4)
        signals["ema_trend"]   = "bullish" if e20 > e50 else "bearish"
        signals["ema_gap_pct"] = round(_safe_divide(e20 - e50, e50) * 100, 3) if e50 != 0 else 0.0
        if ema200 is not None:
            e200 = float(ema200.iloc[-1])
            signals["ema200"]          = round(e200, 4)
            signals["price_vs_ema200"] = "above" if price > e200 else "below"
        else:
            signals["ema200"]          = None
            signals["price_vs_ema200"] = "unknown"

        # ── ADX ───────────────────────────────────────────────────────────
        adx, pos_di, neg_di = _adx(high, low, close)
        adx_val = round(float(adx.iloc[-1]), 2)
        signals["adx"]          = adx_val
        signals["adx_pos_di"]   = round(float(pos_di.iloc[-1]), 2)
        signals["adx_neg_di"]   = round(float(neg_di.iloc[-1]), 2)
        signals["market_regime"] = (
            "strong_trend" if adx_val > 30 else
            "trending"     if adx_val > 20 else
            "ranging"
        )
        signals["di_cross"] = "bullish" if signals["adx_pos_di"] > signals["adx_neg_di"] else "bearish"

        # ── OBV ───────────────────────────────────────────────────────────
        obv = _obv(close, vol)
        obv_ema = _ema(obv, C.EMA_SHORT)
        signals["obv_trend"] = "bullish" if float(obv.iloc[-1]) > float(obv_ema.iloc[-1]) else "bearish"

        # ── VWAP ──────────────────────────────────────────────────────────
        vwap = _vwap(high, low, close, vol)
        vwap_val = float(vwap.iloc[-1])
        if np.isnan(vwap_val) or vwap_val == 0:
            # Fallback when volume data is bad
            vwap_val = price
        signals["vwap"]          = round(vwap_val, 4)
        signals["price_vs_vwap"] = "above" if price > vwap_val else "below"
        signals["vwap_gap_pct"]  = round(_safe_divide(price - vwap_val, vwap_val) * 100, 3) if vwap_val != 0 else 0.0

        # ── Williams %R ───────────────────────────────────────────────────
        wpr = _williams_r(high, low, close)
        wpr_val = round(float(wpr.iloc[-1]), 2)
        signals["williams_r"]      = wpr_val
        signals["williams_r_zone"] = (
            "oversold"   if wpr_val < -80 else
            "overbought" if wpr_val > -20 else
            "neutral"
        )

        # ── Volume ────────────────────────────────────────────────────────
        vol_avg = float(vol.iloc[-C.VOLUME_AVG_WINDOW:].mean())
        vol_cur = float(vol.iloc[-1])
        signals["volume_ratio"] = round(vol_cur / vol_avg, 3) if vol_avg > 0 else 1.0
        signals["volume_spike"] = signals["volume_ratio"] > C.VOLUME_SPIKE_RATIO
        signals["volume_trend"] = (
            "increasing"
            if vol.iloc[-C.VOLUME_TREND_SHORT:].mean() > vol.iloc[-C.VOLUME_AVG_WINDOW:].mean()
            else "decreasing"
        )

        # ── ATR ───────────────────────────────────────────────────────────
        atr = _atr(high, low, close)
        atr_val = float(atr.iloc[-1])
        signals["atr"]     = round(atr_val, 6)
        signals["atr_pct"] = round(_safe_divide(atr_val, price) * 100, 3) if price > 0 else 0.0

        # ── Price Momentum ────────────────────────────────────────────────
        signals["change_1h"]  = round(float(close.pct_change(1).iloc[-1] * 100), 3)
        signals["change_4h"]  = round(float(close.pct_change(4).iloc[-1] * 100), 3)
        signals["change_24h"] = round(float(close.pct_change(24).iloc[-1] * 100), 3)

        # ── Support / Resistance ──────────────────────────────────────────
        recent = df.iloc[-20:]
        signals["support_20"]    = round(float(recent["low"].min()), 4)
        signals["resistance_20"] = round(float(recent["high"].max()), 4)
        signals["current_price"] = round(price, 4)
        signals["dist_to_support_pct"]    = round(_safe_divide(price - signals["support_20"], price) * 100, 2) if price > 0 else 0.0
        signals["dist_to_resistance_pct"] = round(_safe_divide(signals["resistance_20"] - price, price) * 100, 2) if price > 0 else 0.0

        # ── Composite Technical Score ─────────────────────────────────────
        score = 0
        if signals["rsi"] < 30:   score += C.SCORE_WEIGHT_RSI_OVERSOLD
        elif signals["rsi"] < 45: score += C.SCORE_WEIGHT_RSI_MILD_BULL
        elif signals["rsi"] > 70: score += C.SCORE_WEIGHT_RSI_OVERBOUGHT
        elif signals["rsi"] > 55: score += C.SCORE_WEIGHT_RSI_MILD_BEAR

        if signals["stoch_rsi_k"] < 20:   score += C.SCORE_WEIGHT_STOCH_OVERSOLD
        elif signals["stoch_rsi_k"] > 80: score += C.SCORE_WEIGHT_STOCH_OVERBOUGHT
        score += C.SCORE_WEIGHT_STOCH_CROSS if signals["stoch_cross"] == "bullish" else -C.SCORE_WEIGHT_STOCH_CROSS

        score += C.SCORE_WEIGHT_MACD_CROSS if signals["macd_cross"] == "bullish" else -C.SCORE_WEIGHT_MACD_CROSS
        score += C.SCORE_WEIGHT_EMA_TREND  if signals["ema_trend"]  == "bullish" else -C.SCORE_WEIGHT_EMA_TREND

        if signals["di_cross"] == "bullish" and adx_val > 20:
            score += C.SCORE_WEIGHT_ADX_DI
        elif signals["di_cross"] == "bearish" and adx_val > 20:
            score -= C.SCORE_WEIGHT_ADX_DI

        if signals["bb_position"] < 0.2:   score += C.SCORE_WEIGHT_BB_POSITION
        elif signals["bb_position"] > 0.8: score -= C.SCORE_WEIGHT_BB_POSITION

        score += C.SCORE_WEIGHT_OBV  if signals["obv_trend"]     == "bullish" else -C.SCORE_WEIGHT_OBV
        score += C.SCORE_WEIGHT_VWAP if signals["price_vs_vwap"] == "above"   else -C.SCORE_WEIGHT_VWAP

        if signals["volume_spike"]:
            score = round(score * C.SCORE_VOLUME_SPIKE_MULTIPLIER, 1)

        signals["technical_score"] = round(max(-100, min(100, score)), 1)
        signals["technical_bias"]  = (
            "strongly_bullish" if score > 50 else
            "bullish"          if score > 20 else
            "strongly_bearish" if score < -50 else
            "bearish"          if score < -20 else
            "neutral"
        )
        return signals

    # ── News & Sentiment ──────────────────────────────────────────────────

    _BULLISH_KEYWORDS = {
        "surge", "rally", "breakout", "adoption", "bullish", "gain", "record",
        "approve", "approval", "etf", "institutional", "buy", "launch",
        "partnership", "upgrade", "accumulate", "bull", "moon", "pump",
        "outperform", "positive", "growth", "legal", "regulated", "support",
    }
    _BEARISH_KEYWORDS = {
        "crash", "drop", "hack", "ban", "bearish", "loss", "exploit",
        "scam", "sell", "fear", "regulation", "lawsuit", "penalty",
        "warning", "risk", "concern", "dump", "correction", "bear",
        "manipulation", "fraud", "investigation", "fine", "liquidation",
    }
    _HIGH_IMPACT_KEYWORDS = {
        "etf", "sec", "approval", "ban", "hack", "exploit", "bankruptcy",
        "federal", "institution", "blackrock", "fidelity", "coinbase",
        "binance", "regulation", "lawsuit", "arrest",
    }

    def _classify_news(self, title: str, votes_pos: int, votes_neg: int) -> dict:
        words = set(title.lower().split())
        bull_score = len(words & self._BULLISH_KEYWORDS)
        bear_score = len(words & self._BEARISH_KEYWORDS)
        high_impact = bool(words & self._HIGH_IMPACT_KEYWORDS)
        total = (bull_score - bear_score) + (votes_pos - votes_neg) * 0.3
        sentiment = (
            "positive" if total > 0.5 else
            "negative" if total < -0.5 else
            "neutral"
        )
        return {"sentiment": sentiment, "high_impact": high_impact}

    def get_crypto_news(self, pair: str, limit: int = C.NEWS_FETCH_LIMIT) -> list:
        """
        Fetch latest crypto news from CryptoPanic.
        Non-critical: returns [] on any failure.
        """
        currency_map = {
            "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL",
            "BNBUSDT": "BNB", "XRPUSDT": "XRP", "ADAUSDT": "ADA",
        }
        cur = currency_map.get(pair, pair.replace("USDT", ""))
        params = {"currencies": cur, "kind": "news", "public": "true"}
        if config.CRYPTOPANIC_KEY:
            params["auth_token"] = config.CRYPTOPANIC_KEY

        try:
            resp = requests.get(
                "https://cryptopanic.com/api/v1/posts/",
                params=params,
                timeout=C.API_TIMEOUT_SECONDS,
            )
            if resp.status_code != 200:
                logger.warning(f"CryptoPanic returned {resp.status_code}")
                return []
            headlines = []
            for post in resp.json().get("results", [])[:limit]:
                votes = post.get("votes", {})
                classification = self._classify_news(
                    post.get("title", ""),
                    votes.get("positive", 0),
                    votes.get("negative", 0),
                )
                headlines.append({
                    "title":       post.get("title", ""),
                    "published":   post.get("published_at", ""),
                    "sentiment":   classification["sentiment"],
                    "high_impact": classification["high_impact"],
                    "source":      post.get("source", {}).get("title", "unknown"),
                })
            return headlines
        except Exception as e:
            logger.error(f"News fetch failed: {e}")
            return []

    def _news_sentiment_summary(self, news: list) -> dict:
        if not news:
            return {"positive": 0, "negative": 0, "neutral": 0, "high_impact": 0, "bias": "neutral"}
        counts = {"positive": 0, "negative": 0, "neutral": 0, "high_impact": 0}
        for n in news:
            counts[n.get("sentiment", "neutral")] += 1
            if n.get("high_impact"):
                counts["high_impact"] += 1
        total = len(news)
        bull_pct = counts["positive"] / total
        bear_pct = counts["negative"] / total
        bias = (
            "strongly_bullish" if bull_pct > 0.6 else
            "bullish"          if bull_pct > 0.4 else
            "strongly_bearish" if bear_pct > 0.6 else
            "bearish"          if bear_pct > 0.4 else
            "neutral"
        )
        return {**counts, "bias": bias, "total": total}

    def get_fear_greed_index(self) -> dict:
        """Alternative.me Fear & Greed Index — free, no key required."""
        try:
            resp = requests.get(
                "https://api.alternative.me/fng/?limit=3",
                timeout=C.API_TIMEOUT_SECONDS,
            )
            if resp.status_code == 200:
                data = resp.json()["data"]
                current  = data[0]
                previous = data[1] if len(data) > 1 else data[0]
                week_ago = data[2] if len(data) > 2 else data[0]
                val      = int(current["value"])
                prev_val = int(previous["value"])
                week_val = int(week_ago["value"])
                return {
                    "value":        val,
                    "label":        current["value_classification"],
                    "previous":     prev_val,
                    "week_ago":     week_val,
                    "trend":        "improving" if val > prev_val else "worsening",
                    "weekly_trend": "improving" if val > week_val else "worsening",
                    "interpretation": self._interpret_fng(val),
                }
        except Exception as e:
            logger.error(f"Fear & Greed fetch failed: {e}")
        return {"value": 50, "label": "Unknown", "trend": "unknown", "interpretation": "data unavailable"}

    def _interpret_fng(self, val: int) -> str:
        if val <= 10:  return "EXTREME FEAR — historically strongest buy zone"
        if val <= 25:  return "extreme fear — contrarian accumulation zone"
        if val <= 45:  return "fear — cautious market, potential entry opportunity"
        if val <= 55:  return "neutral — no strong sentiment edge"
        if val <= 75:  return "greed — market overheated, consider reducing exposure"
        return "EXTREME GREED — high correction risk, be very cautious"

    # ── Full Snapshot ─────────────────────────────────────────────────────

    def collect_all(self, pair: str) -> dict:
        """
        Master collection method — gathers everything for one pair.
        Raises DataCollectionError if critical price/indicator data is unavailable.
        """
        logger.info(f"[{pair}] Collecting market data...")

        # Critical: raises DataCollectionError if Binance is down
        df_1h = self.get_ohlcv(pair, "1h", 150)
        df_4h = self.get_ohlcv(pair, "4h", 100)

        indicators_1h = self.compute_indicators(df_1h)
        indicators_4h = self.compute_indicators(df_4h)

        if not indicators_1h:
            raise DataCollectionError(f"[{pair}] 1H indicators empty — not enough candle history")

        # Non-critical: failures return safe defaults
        price     = self.get_current_price(pair)
        ob_imbal  = self.get_order_book_imbalance(pair)
        news      = self.get_crypto_news(pair)
        fng       = self.get_fear_greed_index()
        balance   = self.get_usdt_balance()
        stats_24h = self.get_24h_stats(pair)

        # Fall back to last close price if ticker call failed
        if not price:
            price = float(df_1h["close"].iloc[-1])
            logger.warning(f"[{pair}] Using last OHLCV close as current price: {price}")

        snapshot = {
            "pair":                 pair,
            "timestamp":            datetime.now(timezone.utc).isoformat(),
            "current_price":        price,
            "usdt_balance":         balance,
            "order_book_imbalance": ob_imbal,   # may be None if unavailable
            "indicators_1h":        indicators_1h,
            "indicators_4h":        indicators_4h,
            "news":                 news,
            "news_summary":         self._news_sentiment_summary(news),
            "fear_greed":           fng,
            "stats_24h":            stats_24h,
        }

        logger.info(
            f"[{pair}] Collected | Price={price} | RSI={indicators_1h.get('rsi')} "
            f"| Regime={indicators_1h.get('market_regime')} | FnG={fng.get('value')} "
            f"| News={len(news)} ({snapshot['news_summary'].get('bias')})"
        )
        return snapshot
