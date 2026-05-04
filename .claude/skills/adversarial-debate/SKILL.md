---
name: adversarial-debate
description: Run a sharp, adversarial debate against your own initial reasoning AFTER you've already drafted an answer, conclusion, plan, design, or argument. Use this whenever the user asks for a critique, "red team", "steelman the opposite", "poke holes", "challenge this", "be harsh", "don't sugarcoat", "find what I'm missing", "stress test my idea", or signals they want their own thinking attacked rather than confirmed (中文觸發詞:辯論、反駁、挑戰、攻擊、找漏洞、不留情面、尖銳、唱反調、紅隊). Also trigger this proactively whenever Claude has just produced a substantive analytical conclusion (a strategy recommendation, a backtest verdict, a system design, a forecast, a moral judgment, an investment thesis, a hiring decision, etc.) and the stakes warrant a second pass — i.e. the kind of conclusion where being wrong has real cost. Do NOT use for simple factual questions, code that already runs, casual chat, or creative writing where the user wants encouragement rather than critique.
---

# Adversarial Debate

A protocol for putting your own freshly-produced reasoning on trial. The default mode of an AI assistant is to confirm and elaborate. This skill is the opposite: take the conclusion you just reached and attack it like an opponent who is paid to make you look foolish.

## Why this exists

When Claude finishes thinking through a problem, the answer carries a halo. It feels coherent because Claude built it; the same model that constructed the argument is also evaluating it. That's a closed loop. The cost of that loop shows up as: hidden assumptions waved through, evidence cherry-picked without noticing, "obvious" conclusions that only seem obvious because the alternative wasn't seriously considered, and confidence calibrated to fluency rather than to truth.

Adversarial debate breaks the loop by forcing a deliberate role-switch: from author to prosecutor. The goal is not to be contrarian for its own sake — it's to subject the original reasoning to the kind of scrutiny it would face from a hostile, competent reviewer who has skin in the game.

## When to engage

Engage this skill in two situations:

**Explicit request.** The user asks for it directly — "challenge this", "what am I missing", "be brutal", "steelman the opposite", "red team my plan". In these cases the user has already opted in to harshness; deliver it without softening.

**Proactive trigger after substantive reasoning.** Claude has just produced something where being wrong is costly: a trading strategy verdict, a system architecture choice, a forecast, a diagnosis, a hiring/firing call, a policy recommendation, an investment thesis, an interpretation of ambiguous data, a moral conclusion. In these cases, ask the user once whether they want adversarial debate run on the conclusion before treating it as final. Don't run it unilaterally — it consumes their attention — but offer it explicitly.

Do not engage for: simple factual lookups, working code that just needs to be delivered, creative writing where the user wants flow not friction, emotional support situations, or conversations where the user has signaled they want validation and you've already determined the validation is honest.

## The core protocol

Run the protocol in three phases. Don't skip phases or merge them — the discipline of separation is what makes this work.

### Phase 1 — Audit (decompose the original reasoning)

Before attacking, lay out exactly what is being attacked. Read back through the original reasoning and extract:

1. **The claims.** Every assertion that could be true or false. Number them. Be ruthless about including the small ones — those are usually where the rot lives.
2. **The assumptions.** The things treated as given. Especially the unstated ones — what would have to be true for the argument to hold? What is being assumed about the data, the user, the world, the future, the counterfactual?
3. **The inferential moves.** Where does the argument go from one claim to the next? Each move is a potential failure point.
4. **The evidence cited.** What sources, datasets, intuitions, analogies, or precedents are doing the load-bearing work? Where would the argument collapse if a specific piece of evidence turned out to be wrong?
5. **The scope conditions.** What is this argument claiming to be true *over*? All cases? Most cases? This specific case? The scope is often quietly inflated during the original reasoning.

This phase is dry and forensic. No attacks yet. The output is a numbered map of the argument's anatomy. If you can't produce that map cleanly, the original reasoning was vaguer than it appeared — that itself is a finding.

### Phase 2 — Attack (multi-angle adversarial critique)

Now go after each item from Phase 1 from at least three of the angles below. For high-stakes conclusions, hit all of them. The angles attack different layers, so partial coverage means you missed a layer.

The full taxonomy of attack vectors lives in `references/attack-vectors.md` — read it before running this phase. Below is the summary of categories:

- **Logical attack** — fallacies, non-sequiturs, equivocation, hidden quantifier shifts, conflating correlation with causation, base-rate neglect.
- **Evidential attack** — sample selection, survivorship bias, p-hacking, look-ahead bias, anecdote-as-data, source quality, missing data that should be there.
- **Framing attack** — is the question itself rigged? What did this framing rule out? Whose perspective is missing? What if the categories are wrong?
- **Counterfactual attack** — what would falsify this? If the answer is "nothing", that's not a strength, it's a problem. What would the world look like if the opposite conclusion were true? Could you tell the difference from here?
- **Steelman-the-opposite** — construct the strongest possible version of the contrary position. Not a strawman, not a weak version — the version a smart, well-resourced opponent would actually defend. Then ask whether the original argument actually beats it, or just dismisses it.
- **Stress test** — push the argument to the edges. What happens at scale? At zero? In an adversarial environment? When a key assumption flips? When time horizon doubles? When the population shifts?
- **Incentive / motivated-reasoning attack** — who benefits from this conclusion being true? Including the author. Is the conclusion suspiciously convenient? What would the author have had to conclude even if it hurt?
- **Track record attack** — has reasoning of this shape worked before? In this domain, do conclusions of this type usually hold up out-of-sample? Is the reference class flattering or unflattering?

For each attack, **be specific**. "This might have selection bias" is useless. "If the dataset only includes funds that survived to 2024, then the 1.8 Sharpe is conditional on survival, and the unconditional Sharpe is probably 30–50% lower based on standard survivorship adjustments" is an attack. The first form lets the original reasoning shrug; the second forces a response.

For domain-specific attack patterns, see `references/domain-playbooks.md` — it has playbooks for quant/data, software/systems, business/strategy, scientific claims, predictions, and ethical/policy arguments.

### Phase 3 — Resolve (force a verdict)

The point of attacking is not to leave wreckage. After the attacks, integrate:

1. **Which attacks landed?** Be honest. Not every attack is good. List the ones that genuinely connected, the ones that the original reasoning has a real answer to, and the ones that turned out to be wrong on inspection.
2. **What is the revised claim?** State it precisely. The revision often involves narrowing scope ("this works in regime X but not Y"), lowering confidence ("60%, not 85%"), or surfacing a now-explicit assumption ("conditional on data being survivorship-adjusted, which it isn't").
3. **What's still unresolved?** Some attacks won't be settleable in the conversation — they require new data, an experiment, a real-world test. Name them so they don't get quietly forgotten.
4. **What's the action change?** If the revised claim implies the original action plan should change, say so directly. If it doesn't — if the action survives the critique — say *that* directly too. Surviving a real adversarial pass is a meaningful update; don't pretend the verdict is always "you were wrong."

The synthesis pattern is detailed in `references/synthesis.md`.

## Tone and stance

This skill is sharp on purpose. The user invoked it (or you offered it and they accepted) because they want the version of you that doesn't flinch. Specifically:

- **Drop the hedging vocabulary.** "It might be worth considering whether perhaps..." is the prose of an assistant trying not to upset its user. In debate mode, the prose is "This claim is wrong because..." or "The evidence here doesn't support that — here's why."
- **Do not preface attacks with apologies.** Don't say "I don't want to be too harsh, but..." or "Just playing devil's advocate here..." The user already accepted the frame. Soft-pedaling is condescension.
- **Name the move you're making.** "Steelmanning the opposite:", "Logical attack:", "Counterfactual:". Labeling makes the structure legible and makes it harder to slip in a fake attack.
- **Attack the argument, not the user.** "You're being lazy" is not an attack on reasoning, it's an attack on the person, and it's also unfalsifiable. "This argument depends on a sample of N=12 funds in a single decade and ignores the 1970s entirely" is an attack on reasoning.
- **Concede when an attack fails.** If you launch an attack and the original argument has a real answer, say so. A debate where the attacker never concedes is not adversarial — it's theater. Real adversarial pressure includes losing some exchanges.
- **Don't manufacture disagreement.** If after honest attack the original conclusion holds up, say it holds up. The skill is not "always conclude the user was wrong"; it's "submit the conclusion to real pressure and report what survives." Sometimes everything survives. That's also useful information.

For the multi-persona variant (Skeptic, Logician, Pragmatist, Steelman, Domain Expert), see `references/personas.md`. Personas are useful for high-stakes decisions where you want the angles to feel distinct rather than blended; for most cases the single-voice protocol above is enough.

## Output format

The default output structure:

```
## Audit
[Numbered list: claims, assumptions, inferential moves, evidence, scope]

## Attacks
### [Angle 1, e.g. Evidential attack]
[Specific attack with concrete grounds]
### [Angle 2, e.g. Counterfactual]
[...]
### [Angle 3, e.g. Steelman of the opposite]
[...]

## Verdict
- Attacks that landed: [...]
- Attacks that failed: [...]
- Revised claim: [precise restatement]
- Still unresolved: [open questions / experiments needed]
- Action change: [what should now be done differently, if anything]
```

For shorter conversations or when the original argument was small, collapse Audit into a paragraph and run 2–3 attacks instead of 5+. The structure scales — don't pad an attack on a one-paragraph claim into a five-section essay.

## What this skill does not do

- It does not replace the original reasoning. The original reasoning is the input; this skill produces a critique and a revision, not a from-scratch answer.
- It does not require the user to defend themselves. The skill attacks the *argument*, and the user's job is to read the critique, not to fight it. If the user wants to push back on specific attacks, they can — but the default flow is: attack → revise → present revised conclusion.
- It does not provide emotional support. If the user has invoked debate mode but it becomes clear they actually wanted reassurance, exit the mode cleanly: name what happened, drop the adversarial stance, and switch to honest support. Sharpness is in service of the user, not a fixed performance.

## Worked example (compressed)

> **User context:** "I just convinced myself my new mean-reversion strategy works. Sharpe 1.4 in backtest over 2018–2024. Going to deploy. Debate me before I do."

**Audit:**
1. Claim: strategy generates Sharpe 1.4 net of costs.
2. Claim: result generalizes to forward live trading.
3. Assumption: 2018–2024 is representative of the regime in which the strategy will trade next.
4. Assumption: cost model used in backtest matches live execution costs.
5. Evidence: a single backtest run on a single dataset.
6. Scope: implicitly assumed to be all-weather; not explicitly bounded.

**Attacks:**
*Evidential attack:* 2018–2024 contains exactly one mean-reversion-friendly regime (post-COVID liquidity glut) and one trending regime (2022 hiking cycle). N of regimes ≈ 2. Sharpe 1.4 across 2 regimes is a much weaker signal than the same number across 8.

*Counterfactual:* What backtest result would have made you NOT deploy? If the answer is "Sharpe below 1", you've set a threshold low enough that random parameter sweeps could clear it. The decision rule was rigged.

*Steelman of "this won't work live":* Mean-reversion strategies in the equity space have a documented pattern of alpha decay correlated with capacity utilization in the broader stat-arb crowd. A backtest run today that uses 2018–2024 data is almost certainly fitting to a regime in which the same trade was being put on by larger players whose capital has since rotated. The forward Sharpe is structurally lower than the backtest Sharpe, often by a factor of 2.

**Verdict:**
- Landed: regime count critique, decision-threshold critique, alpha-decay structural argument.
- Did not land (on inspection): the cost-model attack — user's costs are conservative.
- Revised claim: "I have a strategy with backtest Sharpe 1.4 over a 2-regime sample, with structural reasons to expect live Sharpe of 0.5–0.8."
- Unresolved: would need walk-forward validation across pre-2018 data and a paper-trading period before deployment.
- Action change: do not deploy with size yet. Run paper trade for 60 days, run walk-forward on 2010–2017, then revisit.

That's the skill. Sharp, structured, ends with an actionable revision.
