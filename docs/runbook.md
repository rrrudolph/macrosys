# Execution runbook

This is the procedure for paper trading, and later for a small live account. The weekly decision job refuses to run if this file changes and nobody records that they reviewed it.

Three kinds of failure are treated differently. Class A can put on a position we did not mean to have, so trading stops and the owner is told. Class B means the data is too stale to decide, so we skip the decision and leave existing positions alone. Class C is a report or a write-up that failed; we log it and keep going.

The owner is the person named in the mandate as allowed to override. "Page" means that person is told immediately (phone or whatever out-of-band channel they have named). This program records the page as an unresolved class-A row in the order database and prints it from `exec status`. It does not send email or SMS by itself.

Commands below are the `exec` subcommands. They need the order database (`--db`) and the kill-switch flag file (`--kill-flag`).

## Class A — stop and page

Anything that can create exposure we did not ask for is class A: a duplicate order, an order whose broker ack was lost (`Unknown`), a reconciliation break we cannot explain, or an illegal order-state change.

What halts: new orders. `exec submit` refuses while a class-A incident is unresolved, and also while the kill switch is on. Existing positions are left in place. Do not "fix" a break by sending another order.

Who is paged: the owner, as soon as the incident is written. Do not wait for the next weekly meeting.

### Reconciliation break

Each day, compare our fill ledger with the broker's positions and cash. In paper, a position is the signed filled amount in USD (a buy of 1,000 adds 1,000 to that instrument), and cash is the running total of those fills starting from zero, not a funded account balance. A buy reduces USD cash by the same amount. A live broker has to be compared on that same basis, or the cash sitting in the account looks like a break. Paper market-on-close orders fill at the next close that is already in the price file, treated as printed at 21:00 UTC.

1. Run `exec reconcile`. If it halts, read the incident. It names the instrument or USD cash, our number, the broker's number, and the difference.
2. A difference that is only an order still working (we have not booked a fill the broker already has, or the reverse, and the gap is no larger than the unfilled amount) is class B. It is a note, not a halt. Poll (`exec poll`) and reconcile again. If the note is still open the next business day, treat it as class A.
3. An unexplained break stays class A. Do not submit. Compare the fill list with the broker's fills for that instrument. Typical causes: a fill we never polled, a fill the broker does not show, or cash that does not match the buys and sells. Correct the ledger only when you can point at a specific fill. Write what you found, then resolve the incident only after the next reconcile is clean. Zero unresolved class-A incidents is a gate for leaving paper (see the checklist below).

### Unknown orders

`Unknown` means we sent an order, or tried to, and do not know if the broker accepted it. That includes a timeout waiting for the acknowledgement.

1. Do not submit the same order again by hand. The client order id is already on file, and a second submit must not create a second order.
2. Run the unknown-order recovery (the OMS `resolve_unknown` path, which `exec poll` does not replace). It asks the broker for that client order id.
3. If the broker says filled, working, or rejected, the ledger adopts that and the incident can close. Check the fill price and quantity against the broker before you treat it as closed.
4. If the broker has no record, leave the order `Unknown`, leave the incident open, and page the owner. Do not assume it was rejected.

### Kill switch

The kill switch is a flag file. While it exists, working orders are cancelled and new submits are refused.

Turn it on: `exec kill --reason "..."` with a short reason (what you saw, not a guess at the market). That cancels Submitted, Working, and PartiallyFilled orders and writes a class-A incident. Orders already in `Unknown` are not force-cancelled; resolve those with the broker lookup above.

Turn it off: `exec unkill --ack "..."`. The acknowledgement has to be a real sentence: what you checked (open orders, positions, the incident). A blank acknowledgement is refused. The sentence is stored in the incident log. Clearing the flag does not close a reconciliation break or an `Unknown` order; those stay halted until they are resolved on their own.

## Class B — skip the decision, keep the book

Use this when data is too stale or too incomplete to decide, and when a reconciliation gap is fully explained by an order that has not finished filling.

What happens: skip this cycle's new trades. Keep the positions you already have. Log the reason (the coverage report, or the class-B pending-fill note). Do not page unless the same gap is still unexplained the next business day — that promotion to class A is deliberate.

## Class C — log only

A report, a narrative, or a hash printout failed, and positions cannot change because of it. Log the error. Fix the report on the next run. Do not halt trading and do not page.

## Monthly kill-switch test

Once a month, in paper, during a quiet window:

1. Note the working orders.
2. `exec kill --reason "monthly paper test"`.
3. Confirm those orders are cancelled and `exec status` shows the kill switch engaged.
4. Try a submit. It must be refused.
5. `exec unkill --ack "monthly test, working orders cancelled, submit refused, book unchanged"`.
6. Confirm the flag is gone and a new paper submit is accepted again.
7. The acknowledgement is the record of the test. If any step fails, leave the switch engaged and treat it as class A.

Do not run this test against a live account until the paper checklist below is done. The live test uses the same steps with the live broker's working orders, at a size the mandate already allows.

## Paper to live-small

Paper has to earn the next stage. All of the following, recorded before the first live order:

- The frozen-forecast ledger has been on for the whole paper period.
- The weekly cycle has run for at least 26 weeks.
- For 8 of those weeks in a row, the cycle finished with no hand-edits to the data.
- Ledger calibration is acceptable under the rule written in the mandate before paper started.
- Paper return after costs is not below the mechanism-free baseline.
- There are zero unresolved class-A incidents, and there were zero class-A incidents in the paper record you are using as the gate. A class-A incident that happened and was later resolved still counts; do not wipe the log to pass the gate.
- Broker connectivity has been tested on the real broker (a separate process, its own credentials, accepting only validated order tickets).
- Daily reconciliation has been run against that broker and has matched within tolerance.
- The kill switch has been tested, including the monthly drill above.
- Live-small, once started, stays at least 26 weeks. The exit test is the same reconciliation and kill-switch checks on live fills, and slippage no worse than the estimate already written down. Size increases only on the steps written in the mandate, never because a month looked good.

## Runbook changes

This file is part of the control. If the text changes, the weekly decision job stops until someone reviews the diff and either passes `--acknowledge-runbook-change` for that run or records the new hash with the runbook hash command. Acknowledging one run does not update the stored hash. After the review is real, record the hash so the next week runs without the flag.
