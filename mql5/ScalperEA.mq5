//+------------------------------------------------------------------+
//|                                                     ScalperEA.mq5 |
//|  EMA cross / pullback scalper with ATR stops, breakeven, trailing  |
//|  stop, time stop and daily risk limits.                           |
//|  Runs on any MetaTrader 5 broker: HFM (HotForex), Deriv MT5, ...  |
//|  Mirrors the Python bot in scalper/strategy.py and scalper/risk.py|
//+------------------------------------------------------------------+
#property copyright   "scalper-bot"
#property version     "1.00"
#property description "EMA/RSI/ATR scalper: ATR SL/TP, breakeven, trailing, time stop, daily loss cap"
#property strict

#include <Trade/Trade.mqh>
#include <Trade/PositionInfo.mqh>
#include <Trade/SymbolInfo.mqh>
#include <Trade/AccountInfo.mqh>

enum ENUM_ENTRY_MODE { ENTRY_CROSS = 0, ENTRY_MOMENTUM = 1 };

input group "=== Strategy ==="
input ENUM_ENTRY_MODE InpEntryMode          = ENTRY_CROSS; // Entry mode
input int             InpEmaFast            = 9;           // Fast EMA
input int             InpEmaSlow            = 21;          // Slow EMA
input int             InpEmaTrend           = 50;          // Trend EMA
input int             InpRsiPeriod          = 14;          // RSI period
input double          InpRsiBuyMin          = 50;          // RSI buy min
input double          InpRsiBuyMax          = 75;          // RSI buy max
input double          InpRsiSellMin         = 25;          // RSI sell min
input double          InpRsiSellMax         = 50;          // RSI sell max
input int             InpAtrPeriod          = 14;          // ATR period
input double          InpSlAtr              = 1.0;         // Stop-loss (x ATR)
input double          InpTpAtr              = 1.5;         // Take-profit (x ATR)
input double          InpMinAtrPoints       = 0;           // Min ATR in points (0 = off)
input double          InpMaxSpreadAtrRatio  = 0.25;        // Max spread / ATR (0 = off)
input int             InpMaxSpreadPoints    = 0;           // Max spread in points (0 = off)
input int             InpMaxHoldBars        = 15;          // Time stop in bars (0 = off)
input double          InpBreakevenAtr       = 0.8;         // Breakeven after x ATR (0 = off)
input int             InpBreakevenBufferPts = 2;           // Breakeven buffer (points)
input double          InpTrailActivateAtr   = 1.0;         // Trailing activates after x ATR
input double          InpTrailAtr           = 0.6;         // Trailing distance (x ATR, 0 = off)
input bool            InpExitOnOppositeCross= true;        // Exit on opposite EMA cross
input double          InpMaxAtrPoints       = 0;           // Max ATR in points (0 = off, skips news spikes)
input int             InpSessionStartHour   = 0;           // Session start hour, server time (0 and 24 = all day)
input int             InpSessionEndHour     = 24;          // Session end hour, server time
input ENUM_TIMEFRAMES InpHtfTimeframe       = PERIOD_CURRENT; // Higher-TF trend filter (PERIOD_CURRENT = off)
input int             InpHtfEma             = 50;          // Higher-TF EMA length

input group "=== Risk ==="
input double          InpRiskPct            = 0.5;         // Risk per trade (% balance)
input double          InpFixedLots          = 0;           // Fixed lots (0 = risk-based)
input double          InpMaxRiskOvershoot   = 2.0;         // Allow min lot up to x intended risk
input double          InpMaxDailyLossPct    = 3.0;         // Max daily loss (% of day-start balance)
input double          InpMaxDailyProfitPct  = 0;           // Daily profit target (% , 0 = off)
input int             InpMaxTradesPerDay    = 30;          // Max trades per day (0 = off)
input int             InpMaxConsecLosses    = 3;           // Pause after N consecutive losses (0 = off)
input int             InpCooldownSec        = 120;         // Cooldown after a loss (seconds)
input int             InpConsecLossPauseSec = 1800;        // Pause length after consecutive losses
input bool            InpPortfolioLimits    = true;        // Daily limits count every chart with this magic
input int             InpMaxTotalPositions  = 3;           // Max open positions across all charts (0 = off)

input group "=== Execution ==="
input ulong           InpMagic              = 777123;      // Magic number
input int             InpDeviationPoints    = 20;          // Max slippage (points)
input string          InpComment            = "scalper";   // Order comment

CTrade        trade;
CPositionInfo position;
CSymbolInfo   symbolInfo;
CAccountInfo  account;

int      hFast = INVALID_HANDLE, hSlow = INVALID_HANDLE, hTrend = INVALID_HANDLE;
int      hRsi  = INVALID_HANDLE, hAtr  = INVALID_HANDLE, hHtf = INVALID_HANDLE;
datetime lastBarTime     = 0;
datetime dayStart        = 0;
double   dayStartBalance = 0;
double   dayPnl          = 0;
int      tradesToday     = 0;
int      consecLosses    = 0;
datetime lastLossTime    = 0;
datetime pausedUntil     = 0;
bool     dealsDirty      = true;
string   lastBlockReason = "";

//+------------------------------------------------------------------+
int OnInit()
{
   if(InpEmaFast >= InpEmaSlow) { Print("InpEmaFast must be smaller than InpEmaSlow"); return INIT_PARAMETERS_INCORRECT; }
   if(InpSlAtr <= 0 || InpTpAtr <= 0) { Print("SL/TP ATR multipliers must be positive"); return INIT_PARAMETERS_INCORRECT; }
   if(!symbolInfo.Name(_Symbol)) { Print("symbol init failed"); return INIT_FAILED; }

   hFast  = iMA(_Symbol, _Period, InpEmaFast,  0, MODE_EMA, PRICE_CLOSE);
   hSlow  = iMA(_Symbol, _Period, InpEmaSlow,  0, MODE_EMA, PRICE_CLOSE);
   hTrend = iMA(_Symbol, _Period, InpEmaTrend, 0, MODE_EMA, PRICE_CLOSE);
   hRsi   = iRSI(_Symbol, _Period, InpRsiPeriod, PRICE_CLOSE);
   hAtr   = iATR(_Symbol, _Period, InpAtrPeriod);
   if(InpHtfTimeframe != PERIOD_CURRENT && InpHtfTimeframe != _Period)
   {
      hHtf = iMA(_Symbol, InpHtfTimeframe, InpHtfEma, 0, MODE_EMA, PRICE_CLOSE);
      if(hHtf == INVALID_HANDLE) { Print("higher-timeframe EMA creation failed: ", GetLastError()); return INIT_FAILED; }
   }
   if(hFast == INVALID_HANDLE || hSlow == INVALID_HANDLE || hTrend == INVALID_HANDLE ||
      hRsi == INVALID_HANDLE || hAtr == INVALID_HANDLE)
   {
      Print("indicator creation failed: ", GetLastError());
      return INIT_FAILED;
   }

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpDeviationPoints);
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetAsyncMode(false);
   trade.LogLevel(LOG_LEVEL_ERRORS);

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
      Print("WARNING: algo trading is disabled in the terminal / EA settings - no orders will be sent");

   RollDay(true);
   PrintFormat("ScalperEA started on %s %s | risk %.2f%% | daily loss cap %.2f%% | magic %I64u",
               _Symbol, EnumToString(_Period), InpRiskPct, InpMaxDailyLossPct, InpMagic);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(hFast  != INVALID_HANDLE) IndicatorRelease(hFast);
   if(hSlow  != INVALID_HANDLE) IndicatorRelease(hSlow);
   if(hTrend != INVALID_HANDLE) IndicatorRelease(hTrend);
   if(hRsi   != INVALID_HANDLE) IndicatorRelease(hRsi);
   if(hAtr   != INVALID_HANDLE) IndicatorRelease(hAtr);
   if(hHtf   != INVALID_HANDLE) IndicatorRelease(hHtf);
}

//+------------------------------------------------------------------+
bool InSession()
{
   if(InpSessionStartHour == InpSessionEndHour) return true;
   if(InpSessionStartHour <= 0 && InpSessionEndHour >= 24) return true;
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   int h = dt.hour;
   if(InpSessionStartHour < InpSessionEndHour) return h >= InpSessionStartHour && h < InpSessionEndHour;
   return h >= InpSessionStartHour || h < InpSessionEndHour;   // overnight session
}

//+------------------------------------------------------------------+
// +1 / -1 trend on the higher timeframe, 0 when the filter is off, -2 when data is not ready.
int HtfBias()
{
   if(hHtf == INVALID_HANDLE) return 0;
   double e[];
   ArraySetAsSeries(e, true);
   if(CopyBuffer(hHtf, 0, 1, 1, e) < 1) return -2;
   double close1 = iClose(_Symbol, InpHtfTimeframe, 1);
   if(close1 <= 0 || e[0] <= 0) return -2;
   return close1 > e[0] ? 1 : -1;
}

//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
{
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD) dealsDirty = true;
}

//+------------------------------------------------------------------+
void OnTick()
{
   if(!symbolInfo.RefreshRates()) return;
   RollDay(false);
   if(dealsDirty) RecomputeDay();

   bool newBar = IsNewBar();
   ManagePositions(newBar);
   if(!newBar) return;
   if(CountOurPositions() > 0) return;
   if(InpMaxTotalPositions > 0 && CountAllOurPositions() >= InpMaxTotalPositions)
   {
      if(lastBlockReason != "max_total_positions") { Print("entries blocked: max_total_positions"); lastBlockReason = "max_total_positions"; }
      return;
   }

   string why;
   if(!RiskAllows(why))
   {
      if(why != lastBlockReason) { Print("entries blocked: ", why); lastBlockReason = why; }
      return;
   }
   lastBlockReason = "";
   CheckEntry();
}

//+------------------------------------------------------------------+
bool IsNewBar()
{
   datetime t = iTime(_Symbol, _Period, 0);
   if(t == lastBarTime) return false;
   bool first = (lastBarTime == 0);
   lastBarTime = t;
   return !first;          // do not act on a stale signal right after start
}

//+------------------------------------------------------------------+
int CountOurPositions()
{
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
      if(position.SelectByIndex(i) && position.Symbol() == _Symbol && (ulong)position.Magic() == InpMagic) n++;
   return n;
}

//+------------------------------------------------------------------+
int CountAllOurPositions()
{
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
      if(position.SelectByIndex(i) && (ulong)position.Magic() == InpMagic) n++;
   return n;
}

//+------------------------------------------------------------------+
double StopsDistance()
{
   double stops = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   return stops + (symbolInfo.Ask() - symbolInfo.Bid());
}

//+------------------------------------------------------------------+
// Reads indicator values for closed bars: index 0 = last closed bar (shift 1).
bool ReadBuffers(double &fast[], double &slow[], double &trend[], double &rsiv[], double &atrv[])
{
   ArraySetAsSeries(fast, true);  ArraySetAsSeries(slow, true);  ArraySetAsSeries(trend, true);
   ArraySetAsSeries(rsiv, true);  ArraySetAsSeries(atrv, true);
   if(CopyBuffer(hFast,  0, 1, 3, fast)  < 3) return false;
   if(CopyBuffer(hSlow,  0, 1, 3, slow)  < 3) return false;
   if(CopyBuffer(hTrend, 0, 1, 3, trend) < 3) return false;
   if(CopyBuffer(hRsi,   0, 1, 3, rsiv)  < 3) return false;
   if(CopyBuffer(hAtr,   0, 1, 3, atrv)  < 3) return false;
   return true;
}

//+------------------------------------------------------------------+
void CheckEntry()
{
   double fast[], slow[], trend[], rsiv[], atrv[];
   if(!ReadBuffers(fast, slow, trend, rsiv, atrv)) return;
   if(Bars(_Symbol, _Period) < MathMax(InpEmaTrend, MathMax(InpRsiPeriod, InpAtrPeriod)) + 5) return;

   if(!InSession()) return;
   double a = atrv[0];
   if(a <= 0) return;
   if(InpMinAtrPoints > 0 && a < InpMinAtrPoints * _Point) return;
   if(InpMaxAtrPoints > 0 && a > InpMaxAtrPoints * _Point) return;
   double spread = symbolInfo.Ask() - symbolInfo.Bid();
   if(InpMaxSpreadPoints > 0 && spread > InpMaxSpreadPoints * _Point) return;
   if(InpMaxSpreadAtrRatio > 0 && spread > InpMaxSpreadAtrRatio * a) return;

   double close1 = iClose(_Symbol, _Period, 1), open1 = iOpen(_Symbol, _Period, 1);
   double low2   = iLow(_Symbol, _Period, 2),   high2 = iHigh(_Symbol, _Period, 2);

   bool longSetup = false, shortSetup = false;
   if(InpEntryMode == ENTRY_CROSS)
   {
      bool crossUp = fast[1] <= slow[1] && fast[0] > slow[0];
      bool crossDn = fast[1] >= slow[1] && fast[0] < slow[0];
      longSetup  = crossUp && close1 > trend[0];
      shortSetup = crossDn && close1 < trend[0];
   }
   else
   {
      bool up = fast[0] > slow[0] && slow[0] > trend[0];
      bool dn = fast[0] < slow[0] && slow[0] < trend[0];
      longSetup  = up && low2  <= fast[1] && close1 > fast[0] && close1 > open1;
      shortSetup = dn && high2 >= fast[1] && close1 < fast[0] && close1 < open1;
   }

   int bias = HtfBias();
   if(bias == -2) return;
   if(bias == 1)  shortSetup = false;
   if(bias == -1) longSetup  = false;

   double r = rsiv[0];
   double minDist = StopsDistance();
   double slDist = MathMax(InpSlAtr * a, minDist);
   double tpDist = MathMax(InpTpAtr * a, minDist);

   if(longSetup && r >= InpRsiBuyMin && r <= InpRsiBuyMax)
      OpenTrade(ORDER_TYPE_BUY, slDist, tpDist, a, r);
   else if(shortSetup && r >= InpRsiSellMin && r <= InpRsiSellMax)
      OpenTrade(ORDER_TYPE_SELL, slDist, tpDist, a, r);
}

//+------------------------------------------------------------------+
double CalcLots(ENUM_ORDER_TYPE type, double entry, double sl)
{
   double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0) step = minLot > 0 ? minLot : 0.01;
   double lots;
   if(InpFixedLots > 0)
      lots = InpFixedLots;
   else
   {
      double riskAmt = account.Balance() * InpRiskPct / 100.0;
      double profit = 0;
      if(!OrderCalcProfit(type, _Symbol, 1.0, entry, sl, profit))
      {
         Print("OrderCalcProfit failed: ", GetLastError());
         return 0;
      }
      double lossPerLot = MathAbs(profit);
      if(lossPerLot <= 0) return 0;
      lots = riskAmt / lossPerLot;
      lots = MathFloor(lots / step) * step;
      if(lots < minLot)
      {
         if(minLot * lossPerLot <= riskAmt * InpMaxRiskOvershoot) lots = minLot;
         else
         {
            PrintFormat("skip: min lot %.2f would risk %.2f > %.2f allowed", minLot, minLot * lossPerLot, riskAmt * InpMaxRiskOvershoot);
            return 0;
         }
      }
   }
   lots = MathMin(lots, maxLot);
   lots = MathMax(lots, minLot);
   int volDigits = (int)MathRound(-MathLog10(step));
   if(volDigits < 0) volDigits = 0;
   return NormalizeDouble(lots, volDigits);
}

//+------------------------------------------------------------------+
void OpenTrade(ENUM_ORDER_TYPE type, double slDist, double tpDist, double a, double r)
{
   bool   isBuy = (type == ORDER_TYPE_BUY);
   double price = isBuy ? symbolInfo.Ask() : symbolInfo.Bid();
   double sl    = NormalizeDouble(isBuy ? price - slDist : price + slDist, _Digits);
   double tp    = NormalizeDouble(isBuy ? price + tpDist : price - tpDist, _Digits);
   double lots  = CalcLots(type, price, sl);
   if(lots <= 0) return;

   bool ok = isBuy ? trade.Buy(lots, _Symbol, price, sl, tp, InpComment)
                   : trade.Sell(lots, _Symbol, price, sl, tp, InpComment);
   uint rc = trade.ResultRetcode();
   if(!ok || (rc != TRADE_RETCODE_DONE && rc != TRADE_RETCODE_PLACED && rc != TRADE_RETCODE_DONE_PARTIAL))
   {
      PrintFormat("order failed: %u %s", rc, trade.ResultRetcodeDescription());
      return;
   }
   tradesToday++;
   PrintFormat("OPEN %s %s lots=%.2f price=%s sl=%s tp=%s atr=%s rsi=%.1f", isBuy ? "BUY" : "SELL", _Symbol, lots,
               DoubleToString(trade.ResultPrice() > 0 ? trade.ResultPrice() : price, _Digits),
               DoubleToString(sl, _Digits), DoubleToString(tp, _Digits), DoubleToString(a, _Digits), r);
}

//+------------------------------------------------------------------+
void ManagePositions(bool newBar)
{
   double fast[], slow[];
   bool haveEma = false;
   if(newBar && InpExitOnOppositeCross)
   {
      ArraySetAsSeries(fast, true); ArraySetAsSeries(slow, true);
      haveEma = CopyBuffer(hFast, 0, 1, 2, fast) == 2 && CopyBuffer(hSlow, 0, 1, 2, slow) == 2;
   }
   double atrv[];
   ArraySetAsSeries(atrv, true);
   double a = (CopyBuffer(hAtr, 0, 1, 1, atrv) == 1) ? atrv[0] : 0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(!position.SelectByIndex(i)) continue;
      if(position.Symbol() != _Symbol || (ulong)position.Magic() != InpMagic) continue;
      bool   isBuy = (position.PositionType() == POSITION_TYPE_BUY);
      double price = isBuy ? symbolInfo.Bid() : symbolInfo.Ask();
      ulong  ticket = position.Ticket();

      if(newBar)
      {
         if(InpMaxHoldBars > 0)
         {
            int held = iBarShift(_Symbol, _Period, position.Time(), false);
            if(held >= InpMaxHoldBars) { ClosePosition(ticket, "max_hold"); continue; }
         }
         if(haveEma)
         {
            if(isBuy  && fast[1] >= slow[1] && fast[0] < slow[0]) { ClosePosition(ticket, "opposite_cross"); continue; }
            if(!isBuy && fast[1] <= slow[1] && fast[0] > slow[0]) { ClosePosition(ticket, "opposite_cross"); continue; }
         }
      }
      if(a <= 0) continue;

      double open  = position.PriceOpen();
      double sl    = position.StopLoss();
      double move  = isBuy ? price - open : open - price;
      double spread = symbolInfo.Ask() - symbolInfo.Bid();
      double newSl = 0;
      bool   modify = false;

      if(InpBreakevenAtr > 0 && move >= InpBreakevenAtr * a)
      {
         double buffer = InpBreakevenBufferPts * _Point + spread;
         double be = isBuy ? open + buffer : open - buffer;
         if((isBuy && sl < be) || (!isBuy && (sl == 0 || sl > be))) { newSl = be; modify = true; }
      }
      if(InpTrailAtr > 0 && move >= InpTrailActivateAtr * a)
      {
         double tr = isBuy ? price - InpTrailAtr * a : price + InpTrailAtr * a;
         if(isBuy)
         {
            double cur = modify ? MathMax(sl, newSl) : sl;
            if(tr > cur) { newSl = tr; modify = true; }
         }
         else
         {
            double cur = (sl == 0) ? DBL_MAX : sl;
            if(modify) cur = MathMin(cur, newSl);
            if(tr < cur) { newSl = tr; modify = true; }
         }
      }
      if(!modify) continue;

      double minDist = StopsDistance();
      if(isBuy  && newSl > price - minDist) newSl = price - minDist;
      if(!isBuy && newSl < price + minDist) newSl = price + minDist;
      newSl = NormalizeDouble(newSl, _Digits);
      if(MathAbs(newSl - sl) < _Point) continue;
      if(!trade.PositionModify(ticket, newSl, position.TakeProfit()))
         PrintFormat("modify %I64u failed: %u %s", ticket, trade.ResultRetcode(), trade.ResultRetcodeDescription());
      else
         PrintFormat("MODIFY %I64u sl=%s (price %s)", ticket, DoubleToString(newSl, _Digits), DoubleToString(price, _Digits));
   }
}

//+------------------------------------------------------------------+
void ClosePosition(ulong ticket, string reason)
{
   if(!trade.PositionClose(ticket))
      PrintFormat("close %I64u failed: %u %s", ticket, trade.ResultRetcode(), trade.ResultRetcodeDescription());
   else
      PrintFormat("CLOSE %I64u reason=%s", ticket, reason);
}

//+------------------------------------------------------------------+
void RollDay(bool force)
{
   datetime now = TimeCurrent();
   datetime ds  = now - (now % 86400);
   if(!force && ds == dayStart) return;
   dayStart = ds;
   lastLossTime = 0;
   pausedUntil  = 0;
   RecomputeDay();
}

//+------------------------------------------------------------------+
// Rebuild today's statistics from the deal history (survives EA restarts).
void RecomputeDay()
{
   dealsDirty   = false;
   tradesToday  = 0;
   dayPnl       = 0;
   consecLosses = 0;
   lastLossTime = 0;
   if(!HistorySelect(dayStart, TimeCurrent() + 86400)) return;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong t = HistoryDealGetTicket(i);
      if(t == 0) continue;
      if(!InpPortfolioLimits && HistoryDealGetString(t, DEAL_SYMBOL) != _Symbol) continue;
      if((ulong)HistoryDealGetInteger(t, DEAL_MAGIC) != InpMagic) continue;
      long entry = HistoryDealGetInteger(t, DEAL_ENTRY);
      double p = HistoryDealGetDouble(t, DEAL_PROFIT) + HistoryDealGetDouble(t, DEAL_COMMISSION)
               + HistoryDealGetDouble(t, DEAL_SWAP) + HistoryDealGetDouble(t, DEAL_FEE);
      if(entry == DEAL_ENTRY_IN)
      {
         tradesToday++;
         dayPnl += p;              // entry commission, if any
      }
      else if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_INOUT || entry == DEAL_ENTRY_OUT_BY)
      {
         dayPnl += p;
         if(p < 0)
         {
            consecLosses++;
            lastLossTime = (datetime)HistoryDealGetInteger(t, DEAL_TIME);
            if(InpMaxConsecLosses > 0 && consecLosses >= InpMaxConsecLosses)
            {
               pausedUntil  = lastLossTime + InpConsecLossPauseSec;
               consecLosses = 0;
            }
         }
         else consecLosses = 0;
      }
   }
   dayStartBalance = account.Balance() - dayPnl;
}

//+------------------------------------------------------------------+
bool RiskAllows(string &why)
{
   datetime now = TimeCurrent();
   if(InpMaxTradesPerDay > 0 && tradesToday >= InpMaxTradesPerDay) { why = "max_trades_per_day"; return false; }
   double base = dayStartBalance > 0 ? dayStartBalance : account.Balance();
   if(InpMaxDailyLossPct > 0 && dayPnl <= -base * InpMaxDailyLossPct / 100.0) { why = "daily_loss_limit"; return false; }
   if(InpMaxDailyProfitPct > 0 && dayPnl >= base * InpMaxDailyProfitPct / 100.0) { why = "daily_profit_target"; return false; }
   if(now < pausedUntil) { why = "consecutive_loss_pause"; return false; }
   if(InpCooldownSec > 0 && lastLossTime > 0 && now - lastLossTime < InpCooldownSec) { why = "cooldown"; return false; }
   why = "ok";
   return true;
}
//+------------------------------------------------------------------+
