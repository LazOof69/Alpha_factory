# Domain Playbooks

Domain-specific attack patterns. The general attack vectors in `attack-vectors.md` work everywhere; these playbooks give you the *specific* failure modes that recur in each domain, so you can attack faster and land harder.

Read the playbook for the relevant domain before running Phase 2 of the protocol. If the conclusion spans multiple domains (e.g., a quant strategy that's also a business decision), read both.

---

## Quant / data analysis / forecasting

The default failure mode is **looking like signal when you're really looking at noise plus selection.**

**Attack checklist:**

- *Survivorship bias.* Does the dataset include things that didn't survive? Funds that closed, companies that delisted, products that failed? If the answer is "no", the result is conditional on survival, and the unconditional version is worse.
- *Look-ahead bias.* Is any information used in the analysis that wouldn't have been available at the relevant decision time? Reconstituted indices, restated financials, "as-of" data that has actually been revised — all common offenders.
- *Selection on the dependent variable.* Did the dataset get filtered using something that's correlated with the outcome? "Stocks with high Sharpe in 2018–2024" filtered for analysis are by construction the Sharpe-survivors.
- *Multiple hypothesis testing / p-hacking.* How many strategies/parameters/variations did you try? If you tried 50 and report the best one, the apparent significance is mostly multiple-comparison artifact.
- *Regime count.* Across how many *distinct regimes* does the result hold? A 6-year window often contains one or two regimes, not six. Sharpe across 1 regime is not Sharpe across the future.
- *In-sample / out-of-sample.* Where was the strategy designed, and where was it tested? If those overlap, the test isn't out-of-sample regardless of what the labels say.
- *Costs and frictions.* Are transaction costs realistic for the live execution venue, instrument, and size? Is slippage modeled? Is borrow modeled for shorts? Is the market-on-close fillable at the close price you used?
- *Capacity decay.* At what AUM does the strategy stop working? If the capacity is small relative to other capital pursuing the same trade, the alpha is being shared.
- *Crowding and reflexivity.* Is the strategy's continued profitability conditional on most other people not running it? If the strategy becomes well-known, what happens?
- *Stationarity.* Is the data-generating process assumed stationary? In markets, almost nothing is. Volatility, correlations, and risk premia all shift.
- *Sharpe-vs-skew tradeoff.* Is the Sharpe high because of low volatility from a strategy that occasionally takes catastrophic losses? Sharpe is a poor metric for negatively-skewed return distributions; check max drawdown, tail ratios, and time-to-recovery.

**Stress tests for quant:**
- 2× transaction costs.
- Apply the strategy to a different but plausibly similar instrument universe — does it still work?
- Run on data prior to the design window — out-of-distribution but not yet "future".
- Inject realistic execution noise (random fills 1–3 ticks worse than assumed).
- Assume capacity is 1/10 of estimate and check whether it still pencils.

---

## Software systems / architecture / infrastructure

The default failure mode is **assuming the happy path is the only path.**

**Attack checklist:**

- *Failure modes.* For each component, what happens when it fails? Slowly fails? Lies? Returns stale data? The argument usually addresses "if it goes down" — does it address "if it returns wrong answers"?
- *Concurrency.* Are race conditions and reordering possible? What happens under contention? At a million concurrent users? At one user but during a network partition?
- *Operational toil.* What does this system require from operators on a Tuesday morning at 3am? "It just works" is rarely true at scale.
- *Vendor lock-in.* What's the exit cost if the chosen vendor or library disappears, raises prices, or changes terms?
- *Data migration.* How does this design handle schema evolution? Backfills? The next change?
- *Security.* What does the trust boundary look like? Where is user input handled? What's the worst thing a hostile user can do?
- *Cost at scale.* What does this cost at 10× current load? At 100×? Cloud bills are nonlinear.
- *Observability.* When this fails in production at 2am, what data exists to diagnose it? "We'll add logs later" almost always means "we'll be flying blind."
- *Backwards compatibility.* What happens to existing clients/users when this rolls out?
- *Premature optimization vs deferred risk.* The argument may be that "we'll fix that later" — is "later" actually achievable, or does the early choice lock it in?

**Stress tests for systems:**
- Network partition between any two components.
- 10× expected traffic.
- A single dependency goes down for 4 hours.
- A bad deploy ships at peak load.
- The on-call engineer is new and the original author has left the company.

---

## Business strategy / product decisions

The default failure mode is **mistaking your enthusiasm for market signal.**

**Attack checklist:**

- *Demand validation.* What evidence is there that customers want this *enough to pay or switch*, not just that they say it sounds nice? Surveys vastly overstate revealed demand.
- *Competitive response.* If this works, what does the incumbent do? What happens to the proposal under a hostile competitor with 100× the resources?
- *Distribution.* How will customers find this? Distribution is usually the bottleneck, not product. Has the proposal solved distribution, or hand-waved it?
- *Unit economics.* At what scale does this become profitable, and is that scale achievable with the available capital?
- *Lifetime value vs CAC.* Do the assumed retention and acquisition cost numbers come from comparable companies, or are they aspirational?
- *Market sizing.* Is the TAM real, or is it the result of multiplying optimistic numbers? "If we capture 1% of a $100B market" is a tell.
- *Path dependency.* What does the proposal commit the company to that's hard to reverse? Hires? Brand positioning? Regulatory disclosures?
- *Founder/team capability.* Does the team have the relevant scar tissue for *this* domain, not just startups in general?
- *Regulatory/legal risk.* What licenses, regulations, or contracts could change in ways that kill this?
- *Build vs buy vs partner.* Was that comparison done seriously, or did "build" win by default?

**Stress tests for business:**
- 2× longer to revenue than projected, 2× higher CAC.
- The biggest competitor announces the same product in 6 months.
- The largest assumed customer segment turns out to be 1/3 the size projected.
- The team's strongest member leaves in month 8.

---

## Scientific or empirical claims

The default failure mode is **confusing "consistent with the data" for "supported by the data."**

**Attack checklist:**

- *Effect size vs statistical significance.* Is the effect both real *and* large enough to matter? Many published findings are statistically significant but practically negligible.
- *Replication.* Has this been replicated independently? In a different lab? With pre-registration?
- *Sample size and power.* Was the study powered to detect the effect it claims? Underpowered studies that find effects are usually finding noise.
- *Pre-registration.* Was the hypothesis registered before the data was seen? If not, the analysis was selected from a garden of forking paths.
- *Mechanism.* Is there a plausible mechanism, or is the result purely correlational? "We have no idea why" is a flag.
- *Out-of-sample predictions.* Has the theory made successful predictions about cases not used to construct it? Theories that only post-hoc fit known data are weak.
- *Alternative explanations.* What other hypotheses could explain the same data? Has the argument ruled them out?
- *Effect heterogeneity.* Does the effect hold across subgroups? If the average effect is real but only one subgroup carries it, the headline claim is misleading.
- *Conflict of interest / funding source.* Does this matter for the result? Sometimes it doesn't; sometimes it does.
- *Publication bias.* Is this a positive result published from a field where negative results don't get published?

**Stress tests for scientific claims:**
- Apply the same methodology to a control case where the effect should be zero — does the methodology produce a "significant" result anyway?
- Halve the sample size and see if the conclusion survives.
- Look for the most adversarial review of this finding by a competent skeptic in the field.

---

## Predictions and forecasts

The default failure mode is **confidence that doesn't track accuracy.**

**Attack checklist:**

- *Calibration history.* What's the forecaster's track record on similar predictions? Calibrated 80%-confident predictions should come true 80% of the time.
- *Reference class.* What's the base rate for predictions of this type? "X is going to happen" predictions in this domain — what's the historical hit rate?
- *Time horizon.* Forecasts get worse fast with horizon. A 1-year forecast and a 10-year forecast are different epistemic objects.
- *Tail risk asymmetry.* Is the forecast point-estimate, or distributional? A point forecast that ignores tails is a poor input to decisions where tails matter.
- *Reflexivity.* Does the act of making the forecast change the forecast? (Very common in markets, politics, technology adoption.)
- *Black swan blindness.* Are there outcomes the forecast doesn't even include in its outcome space? "It can't happen" forecasts have a poor record.
- *Anchoring.* Is the forecast anchored to the present? Most forecasts implicitly say "the future will look like now plus a small adjustment."
- *Aggregation.* Has the forecast been compared to a market price, prediction-market price, or expert aggregation? If they disagree strongly, why are you right and they wrong?

**Stress tests for forecasts:**
- What 2σ event would falsify this? What's its probability under your model? Does that match your gut?
- Run the forecast as a distribution, not a point estimate. Where's the 95% interval?
- Imagine you're wrong — what's the most likely *way* you're wrong? Your forecast should have already accounted for that path.

---

## Ethical / policy / normative arguments

The default failure mode is **applying a moral principle as if it had no costs.**

**Attack checklist:**

- *Whose costs are counted?* Most moral arguments implicitly weight some parties more than others. Surface the weights.
- *Symmetry test.* Apply the principle in question to a case where you don't already have intuitions. Does it still feel right?
- *Reversal test.* If the situation were reversed — different group, different stakes, opposite political valence — would you reach the same conclusion?
- *Marginal vs absolute.* Is this principle being applied marginally (more is better) or absolutely (a binary)? Most moral principles work as one but not the other.
- *Slippery slope vs. principle.* Is the argument really about *this* case, or about "if we allow this, we'll have to allow that"? The slope claim is a separate empirical question and deserves separate evidence.
- *Counterexample.* What's the strongest case where applying this principle gives a clearly wrong answer? How does the argument handle it?
- *Veil of ignorance.* If you didn't know which side of this you'd land on, would you still endorse the principle?
- *Practical implementability.* Even if the principle is right in the abstract, what does enforcement look like? Who pays the enforcement cost? Are the side effects worse than the original problem?
- *Coalitional thinking.* Is the conclusion suspiciously aligned with what the author's tribe already believes? That's a flag for motivated reasoning, not by itself a refutation.

**Stress tests for normative claims:**
- Maximum-extension test: apply the principle universally. What does the world look like? Is that world acceptable?
- Adversarial-application test: a hostile party uses this principle as cover for something the original author would oppose. Does the principle still hold up, or does it need a clarification?

---

## Personal decisions / life choices

The default failure mode is **confusing how you feel about it now with how you'll feel about it later.**

**Attack checklist:**

- *Reversibility.* If this turns out wrong, can you reverse it? If yes, the bar is lower. If no, the bar should be much higher than it currently is.
- *Optionality.* Does this decision preserve future options or close them?
- *Sunk cost.* Is the argument secretly "I've already invested so much"? That isn't a reason; that's a bias.
- *Identity protection.* Is the conclusion serving the story you tell about yourself, even at the cost of being right?
- *Hyperbolic discounting.* Is the choice favoring short-term comfort/discomfort over long-term value, or vice versa?
- *Outside view.* What would a thoughtful friend, with no skin in the game, say about someone in this situation considering this choice?
- *Counterfactual self.* Imagine the version of you that didn't make this decision — what would they tell you about it 5 years from now?
- *Trusted-advisor test.* If you had to defend this decision to someone whose judgment you respect and who would push back, what would you say?

**Stress tests for personal decisions:**
- Imagine the decision turns out badly. What's the regret narrative? How likely is it?
- Imagine the decision turns out well. What was the actual mechanism — was it the decision itself, or external factors that would've helped any choice?
- Run a pre-mortem: "It's two years from now and this went wrong. What went wrong?"

---

## When the domain doesn't fit

If the conclusion spans an unusual domain, default to:

1. Find the closest domain in the playbooks above.
2. Use that domain's checklist as a starting point.
3. Cross-reference with the general taxonomy in `attack-vectors.md`.
4. Look for what's unique about the actual domain that the analogous domain misses.

The playbooks aren't exhaustive — they're starting points that beat starting from scratch.
