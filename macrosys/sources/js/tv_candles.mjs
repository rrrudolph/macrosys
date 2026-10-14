// Unofficial @mathieuc/tradingview v4 runner. One JSON object on stdin, one candle JSON per stdout line.
// TV_SESSION / TV_SIGNATURE are copied into getCandles({credentials}) and never written out.
// TradingView's terms govern use. These rows are research-tier only.

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const req = JSON.parse(Buffer.concat(chunks).toString("utf8"));

const { getCandles } = await import("@mathieuc/tradingview/data");

const query = { symbol: req.symbol, timeframe: req.timeframe };
if (req.count != null) query.count = req.count;
if (req.from != null) query.from = req.from;
if (process.env.TV_SESSION) {
  query.credentials = { session: process.env.TV_SESSION };
  if (process.env.TV_SIGNATURE) query.credentials.signature = process.env.TV_SIGNATURE;
}

const candles = await getCandles(query);
for (const candle of candles) {
  process.stdout.write(
    `${JSON.stringify({
      time: candle.time,
      open: candle.open,
      high: candle.high,
      low: candle.low,
      close: candle.close,
      volume: candle.volume,
    })}\n`,
  );
}
