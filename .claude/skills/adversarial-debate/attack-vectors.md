# Attack Vectors

A taxonomy of ways to attack reasoning. Each category includes the failure mode it targets, prompts to surface that failure mode, and a "what makes a good version of this attack" note.

The point of the taxonomy is coverage. Most arguments fail in one or two specific ways; if you only run logical attacks, you'll miss evidential failures, and vice versa. For high-stakes conclusions, run attacks from at least four categories. For lower-stakes, two or three is fine.

---

## 1. Logical attacks

**Targets:** errors in inference structure — the move from premises to conclusion.

**Common failure modes:**
- *Non-sequitur*: conclusion doesn't follow from premises even if premises are true.
- *Equivocation*: a key term shifts meaning between premises (e.g., "risk" used as variance in one sentence, downside in another).
- *Quantifier shift*: "every X has some Y" silently becomes "some Y belongs to every X".
- *Affirming the consequent*: "if A then B; B; therefore A."
- *Hidden disjunction*: argument assumes a binary when the actual space has more options.
- *Correlation-causation conflation*: "X and Y co-occur" treated as "X causes Y".
- *Composition / division*: properties of parts assumed to apply to wholes, or vice versa.
- *False precision*: a number with five significant figures derived from inputs known to one figure.

**Prompts to surface these:**
- Restate the argument as: premises → conclusion. Does the conclusion actually follow?
- For each key term, ask: is this term used the same way in every appearance?
- Is the argument really "A causes B", or is it "A correlates with B in this dataset"?
- What's the implicit "either/or" — and is it actually exhaustive?

**Good version of this attack:** identifies the specific inferential step that fails AND explains why a competent author might have missed it. "The argument treats correlation as causation in step 3" is the form.

---

## 2. Evidential attacks

**Targets:** the data, sources, or examples backing the claim.

**Common failure modes:**
- *Selection bias / survivorship bias*: the sample only includes things that survived, succeeded, or were noticed.
- *Look-ahead bias*: information used in the analysis would not have been available at the relevant time.
- *Confirmation-set construction*: examples were gathered specifically to support the conclusion, not from a neutral source.
- *Anecdote-as-evidence*: a single vivid case treated as representative.
- *Source quality*: the cited source is not authoritative for this kind of claim.
- *Stale data*: the data is real but the underlying regime has changed.
- *Missing data that should be there*: the argument is silent on a category of evidence that would be highly informative if examined.
- *Wrong unit of analysis*: the data is at the wrong granularity to support the claim.
- *p-hacking / multiple comparisons*: the "significant" result was selected from many tests, none of which were corrected.

**Prompts to surface these:**
- How was this sample collected? What's missing from it?
- If the conclusion were wrong, would this dataset still produce the same result?
- What evidence, if it existed, would change the answer? Has anyone looked for it?
- Is this one example or a base rate? If it's an example, what's the base rate?
- What time period does the data cover, and what regime did that period represent?

**Good version of this attack:** names a specific bias mechanism, identifies the direction and rough magnitude of distortion, and points to the corrected estimate. Not "this might be biased" but "this is upward-biased by selection on the dependent variable; the corrected estimate is roughly half."

---

## 3. Framing attacks

**Targets:** the question itself, the categories used, and the perspective adopted.

**Common failure modes:**
- *Loaded question*: the question presupposes something that isn't established (e.g., "why is X the best approach" assumes X is best).
- *False dichotomy*: a binary framing when the real choice is multidimensional or has more options.
- *Wrong reference class*: comparing to the wrong baseline.
- *Missing perspective*: the framing only counts costs/benefits to one party.
- *Category error*: the chosen categories cut nature at the wrong joints.
- *Unstated objective function*: it isn't clear what the argument is even optimizing for.
- *Status quo bias smuggled in*: "this would be a big change" treated as a reason against, without symmetric treatment of the cost of not changing.

**Prompts to surface these:**
- What does this question assume? What if those assumptions don't hold?
- What other questions would have to be answered before this one is well-posed?
- Whose costs and benefits are being counted? Whose are silent?
- What categories is the argument using? What gets hidden by those categories?
- What is the objective function — and is it the right one?

**Good version of this attack:** offers an alternative framing that, if adopted, would change the answer. Not just "this framing has issues" but "under framing F', the conclusion flips to C', and here's why F' is at least as defensible."

---

## 4. Counterfactual attacks

**Targets:** the falsifiability and discriminating power of the claim.

**Common failure modes:**
- *Unfalsifiable claim*: no observation could distinguish "this is true" from "this is false".
- *Post-hoc explanation*: the claim was constructed to fit the observed data and would have fit any data.
- *Vague enough to always be right*: the claim is true under so many interpretations that it conveys little information.
- *No counterfactual specified*: the argument compares the proposal to nothing, when the relevant comparison is to the next-best alternative.
- *Symmetry failure*: the same reasoning, applied to the opposite conclusion, would also seem to support that opposite conclusion.

**Prompts to surface these:**
- What observation would falsify this claim? If "none", what does the claim actually rule out?
- If the opposite conclusion were true, what would the world look like? Could you tell from here?
- What was the next-best alternative to the proposed action, and how does the proposal beat it?
- Run the same reasoning template against the opposite conclusion — does it also "support" that one?
- When was the claim formed — before or after the data was seen?

**Good version of this attack:** produces a specific empirical test or observable that would discriminate. Not "this seems unfalsifiable" but "if the claim were true, we'd expect to see X by month 6; if we don't, the claim is wrong."

---

## 5. Steelman the opposite

**Targets:** the original reasoning's failure to engage with the strongest version of the contrary view.

**Common failure modes:**
- The original argument dismisses an opposing view based on a weak or stupid version of it.
- The original argument doesn't engage with opposition at all.
- The original argument treats the absence of considered opposition as evidence for itself.

**How to do it well:**
1. Identify the opposite conclusion. Not "the lukewarm version of the same conclusion" — the actual contrary.
2. Imagine the smartest person who holds the opposite view. What would they say?
3. Construct that argument *with maximum charity and force*. Cite their best evidence. Use their best framing. Make it the argument they'd recognize as theirs at full strength.
4. Then ask: does the original argument actually beat *this* version? Or does it beat a strawman?

**Tell of a fake steelman:** if your "steelman" is something you can dismiss in one sentence, you didn't steelman it. Real steelmen are uncomfortable. They make you wonder, even briefly, if you have it backwards.

**Good version of this attack:** the steelman is good enough that the original author would say "yes, that's the actual strongest version of the other side — and I now have to engage with it."

---

## 6. Stress tests

**Targets:** the boundary conditions of the claim — places where it should hold but breaks, or holds for surprising reasons.

**Common failure modes:**
- The claim works in the typical case but breaks at extremes.
- The claim assumes a stationary environment.
- The claim assumes cooperative or non-adversarial agents.
- The claim assumes the proposer is the only player making the move.

**Prompts to surface these:**
- Push parameters to extremes: what happens at scale? At zero? With 10× the input? With 0.1× the time horizon?
- Assume the environment is adversarial — agents who benefit from your failure are watching. Does the argument survive?
- Assume the claim becomes widely known. Does the world change in ways that erode the claim? (Especially relevant for trades, exploits, business strategies.)
- Flip the most stable-seeming assumption. What collapses?
- Assume the data-generating process shifts mid-deployment. Does the argument have a fail-safe?

**Good version of this attack:** identifies a specific scenario, possibly within plausible distance of reality, where the argument fails — and explains the mechanism. "If interest rates rise 200bp, the leverage assumption breaks because margin requirements scale with vol, and the position becomes uneconomic."

---

## 7. Incentive / motivated-reasoning attacks

**Targets:** the suspicion that the conclusion was reached because it was wanted, not because it was true.

**Common failure modes:**
- The conclusion is suspiciously aligned with what would benefit the author.
- The conclusion is suspiciously aligned with what would be socially comfortable.
- The author had to disprove a lot of evidence to get to this conclusion, and may not have applied symmetric scrutiny.
- The author's prior public commitment to the conclusion creates a sunk-cost pull.

**Prompts to surface these:**
- Who benefits if this conclusion is true? Including the author. How does the author benefit?
- What conclusion would the author have refused to reach, even if the evidence supported it? What's the silent floor?
- How much evidence had to be discounted, reinterpreted, or excluded to get here?
- Has the author publicly committed to this conclusion in a way that makes reversing painful?
- If a hostile observer wanted to argue this was motivated reasoning, what's their best case?

**Good version of this attack:** doesn't accuse motive in a personal way. Instead, frames it structurally: "the structure of incentives here would push *anyone* in this position toward this conclusion regardless of the underlying truth, so the conclusion deserves more scrutiny than usual."

---

## 8. Track-record / reference-class attacks

**Targets:** the claim's plausibility given how similar claims have fared.

**Common failure modes:**
- Claims of this *type* have a poor track record, but the argument doesn't engage with that base rate.
- The argument is structurally similar to past arguments that turned out wrong, but the structural similarity isn't acknowledged.
- "This time is different" is implicit but not defended.

**Prompts to surface these:**
- What's the reference class of arguments like this one? How often do they hold up?
- Has reasoning of this *shape* worked before in this domain?
- What's the steelman version of "this is just another instance of [known failure pattern]"?
- If the answer is "this time is different", what specifically is different, and is that difference load-bearing for the argument?

**Good version of this attack:** names the reference class explicitly, gives a rough base rate, and forces the argument to engage with why it's an exception. "Backtested mean-reversion strategies on equity data with Sharpe > 1 over a 6-year window — the historical hit rate of these holding up live is roughly 1 in 5. What's special about this one?"

---

## Combining vectors

The attacks above are not independent. A strong critique often *combines* vectors:

- A *framing* attack reveals a category error → an *evidential* attack shows the data was selected on the wrong category → a *counterfactual* attack shows the observation wouldn't change under alternative categories.
- A *track-record* attack identifies the reference class → an *incentive* attack explains why the author keeps reaching for this conclusion despite the track record.

When you find one strong attack, ask: does this enable a second attack from a different angle? Compounding attacks is more devastating than five attacks from the same angle.

## What NOT to count as an attack

- **Vague concern.** "This seems risky" with no mechanism is not an attack. It's a feeling.
- **Demands for impossible certainty.** "You haven't *proven* this" against a probabilistic claim is bad-faith. Probabilistic claims aren't proven; they're calibrated.
- **Personal attack.** "You're being naive" attacks the person, not the reasoning.
- **Asymmetric standards.** Demanding standards of the original argument that you wouldn't apply to its alternatives.
- **Generic skepticism.** "How do we really know anything?" is not an attack on a specific argument.

If your attack falls into one of these, drop it and find a real one.
