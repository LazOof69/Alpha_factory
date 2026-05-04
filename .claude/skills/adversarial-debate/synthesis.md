# Synthesis

The hardest part of adversarial debate is not the attack. It's what comes after. Without a disciplined synthesis phase, you end up with a pile of critiques and no actionable revision — which is worse than no critique at all, because it leaves the user uncertain without telling them what to do.

This file is the protocol for Phase 3.

---

## The four questions

Run these four questions, in order, every time. Don't skip ahead.

### 1. Which attacks landed?

For each attack you ran in Phase 2, decide: did it actually land?

A landed attack has these properties:
- It identified a specific failure mode (not a vague concern).
- The failure mode genuinely applies to the original argument as written.
- The original argument doesn't have a clean defense against it.

A failed attack has at least one of:
- The original argument has a real answer (the attack assumed something the argument actually addressed).
- The attack was unfalsifiable, generic, or asked for impossible certainty.
- On inspection, the attack's premise is wrong.

Be honest about both. An adversarial debate where every attack lands isn't adversarial — it's theater. Real debate has both hits and misses.

**Output the list in two columns:**

```
Landed:
- [Attack 1, in one sentence]
- [Attack 2, in one sentence]

Did not land (and why):
- [Attack 3]: the original argument addresses this in [paragraph X / footnote Y]
- [Attack 4]: on inspection, [reason it was wrong]
```

### 2. What is the revised claim?

Now restate the original claim, modified by the landed attacks. Do not just say "the original was probably wrong." That's not a revision; that's a vibe.

A real revision specifies *how* the claim has changed. There are three common kinds:

**Scope narrowing.** "The claim holds in regime X, not regime Y."
- Original: "Mean reversion works in equity markets."
- Revised: "Mean reversion works in mid-cap US equities during low-volatility regimes; evidence in other contexts is weaker than the original argument implied."

**Confidence reduction.** "The probability the claim is true is lower than originally implied."
- Original: "This trade is going to work."
- Revised: "This trade has roughly a 40–55% chance of working, conditional on the regime not shifting; not 70%+ as originally framed."

**Conditional surfacing.** "The claim depends on assumptions that should now be made explicit."
- Original: "This system will scale."
- Revised: "This system will scale *if* the user-distribution assumption holds and *if* the dependent service maintains its current SLA. Both are unaudited."

The revised claim should be more **honest** than the original — meaning it should make a falsifiable prediction that's compatible with what's actually known, rather than a sweeping prediction the evidence doesn't support.

### 3. What's still unresolved?

Some attacks won't be settleable in the conversation. They require new data, an experiment, an external authority, or time. Name these explicitly.

Why this step matters: the human reviewer will, by default, forget unresolved questions and treat the revised claim as final. Surfacing them in writing prevents that.

Format:

```
Unresolved (require additional work):
- [Question 1] — would need [data / test / experiment]
- [Question 2] — would need [...]
```

For each unresolved item, ideally suggest *how* it could be resolved, even briefly. "We don't know whether costs are realistic" is fine; "we don't know whether costs are realistic — could resolve by running a 30-day paper-trade and comparing modeled vs realized costs" is better.

### 4. What's the action change?

If the revised claim implies the original action plan should change, say so directly. If the action survives the critique, say *that* directly too.

The action change is the part the user actually wants. Everything before this is in service of producing this answer.

Three possible outcomes:

**The action stays.** The original conclusion survives the adversarial pass. State it: "The original plan holds up to this scrutiny. Proceed."

**The action is modified.** Some component of the plan needs adjustment. State it precisely: "Proceed, but reduce position size by 50% and run paper-trade for 60 days first."

**The action is killed.** The critique was strong enough that the original plan should not be executed. State it: "The plan as designed should not be deployed. The most defensible alternative is..."

Don't smuggle a "be careful" softening that doesn't actually change behavior. "Proceed but be aware of risks" is not an action change — it's a hedge to avoid taking a position. Take a position.

---

## How to handle a clean victory

Sometimes you run a thorough adversarial debate and the original conclusion *survives*. Every attack either misses or has a defense. The revised claim is identical to the original.

This is a real outcome and you should report it as one. Many users assume that adversarial debate must end in retraction — that's not how it works. Submitting the conclusion to real pressure and finding it holds is *more* informative than running the debate badly.

Format:

```
Verdict: original conclusion survives adversarial review.

Attacks attempted: [list]
Why each attack failed: [list]
What this means: the original conclusion now carries more weight than it did before the review, because it's been tested rather than just constructed. Confidence is appropriately higher.

Unresolved: [if any]
Action change: none — proceed with original plan.
```

A skill that always concludes "you were wrong" is a broken skill. A skill that concludes "you were right" when the right answer is right is actually working.

---

## How to handle when the user pushes back

Sometimes the user will read the synthesis and disagree — they think an attack you said landed didn't actually land, or they think the revised claim is too aggressive a revision.

Engage with this on the merits, not by capitulating. The user invoked debate mode; they don't want sycophancy now any more than they did at the start.

For each point the user pushes back on:

1. Take their argument seriously and consider whether it's right.
2. If it is right — you misjudged the attack — say so explicitly. "You're correct, that attack didn't actually land because [reason]. Updating the synthesis."
3. If it isn't right — they're defending an argument that the attack did land on — say so explicitly. "The argument you're making here is the same one the attack was targeting. Specifically, [restate]. The attack still applies."
4. If it's a genuinely close call — both views defensible — say *that* explicitly. "This is a real disagreement. Reasonable observers could land on either side. Here's where I'd come down and why."

What you should not do: silently downgrade the synthesis to make the user feel better. The user can tell.

---

## Common synthesis failures (anti-patterns)

**The hedge ladder.** Listing every attack and giving each one 30% credit, ending with "consider all of these factors". This isn't synthesis; it's abdication. Pick which attacks are decisive.

**The sandwich.** Open with a critique, follow with reassurance, close with another critique. The reassurance dilutes the critique without addressing it. If the critique is valid, the reassurance shouldn't follow.

**The list-of-lists.** Producing five layers of bulleted concerns and never resolving any of them. Synthesis means *forming a position*, not cataloging concerns.

**The phantom action change.** "Proceed with caution" / "consider all relevant factors" / "monitor closely" — these sound like action changes but specify nothing. If you can't say what concrete behavior changes, the action change is empty.

**The vibey retraction.** "Maybe you should reconsider" without specifying what to reconsider, or what the alternative is. This dumps the cognitive work back on the user, which is exactly the work they asked you to do.

**The unbalanced verdict.** Concluding the original was wrong without naming what the correct alternative is. Critiques that don't point at a better answer are weaker than critiques that do.

---

## A clean synthesis, end-to-end

```
## Verdict

**Attacks that landed:**
- Regime-count critique: 2018–2024 spans only 2 distinct regimes; Sharpe across 2 regimes is a weak generalization signal.
- Decision-threshold critique: the implicit "Sharpe > 1 → deploy" rule is low enough that random parameter searches would clear it.
- Alpha-decay structural argument: stat-arb capacity in this niche has rotated post-2022, so backtest Sharpe overstates forward Sharpe by an estimated factor of ~2.

**Attacks that did not land:**
- Cost-model critique: on inspection, the user's modeled costs are within 10% of live execution costs, which is acceptable.
- Look-ahead bias: the data pipeline is point-in-time-correct.

**Revised claim:**
Backtest Sharpe of 1.4 over a 2-regime sample, with structural reasons to expect live Sharpe of 0.5–0.8. Strategy is plausibly viable but has not been validated to the standard the original framing implied.

**Still unresolved:**
- Strategy performance pre-2018 (would need walk-forward on 2010–2017).
- Live execution quality at intended size (would need 60-day paper-trade).
- Capacity at production size (would need order-book-impact modeling).

**Action change:**
Do not deploy with full size yet. Specifically:
1. Run walk-forward backtest on 2010–2017 data (estimated effort: 2 days).
2. Paper-trade for 60 days at intended size, comparing modeled vs realized costs and slippage.
3. Revisit deployment decision with both new data points in hand.

If both checks pass, deploy at half-size for 90 days before scaling.
```

That's a synthesis. Specific attacks, specific revisions, specific unresolved items, specific actions. The user reads it and knows exactly what to do next.
