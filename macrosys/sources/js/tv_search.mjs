// searchMarkets runner for @mathieuc/tradingview v4.0.0. Stdin: {query, type?}. Stdout: one JSON array.
// v4 SearchMarketsOptions does not take credentials; TV_SESSION and TV_SIGNATURE stay in the environment.

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const req = JSON.parse(Buffer.concat(chunks).toString("utf8"));

const { searchMarkets } = await import("@mathieuc/tradingview/data");
const options = {};
if (req.type) options.type = req.type;
const markets = await searchMarkets(req.query, options);
process.stdout.write(`${JSON.stringify(markets)}\n`);
