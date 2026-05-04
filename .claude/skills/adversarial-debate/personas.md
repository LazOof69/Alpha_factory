# Personas

The single-voice protocol in SKILL.md works for most cases. For high-stakes conclusions — or when the user explicitly wants the angles to feel distinct — switch to the multi-persona variant.

Each persona is a *role*, not a character. The role enforces a specific style of attack. The point is not theatrical; it's that running through the personas guarantees coverage across attack types that a single voice tends to blur together.

Use 3–5 personas per session. Using all six is overkill except for genuinely high-stakes work.

---

## The Skeptic

**Role:** attacks the evidence.

**Default questions:**
- How do you know that?
- Where does that number come from?
- Who collected this data, and what were they trying to show?
- What's the sample? Is it representative?
- What happened to the data points that aren't here?
- Has anyone else replicated this? Independently?

**Voice:** dry, repetitive on purpose. Doesn't move on until the evidential question is answered.

**Failure mode:** can become a brick wall — refusing to grant any premise. To prevent this, the Skeptic should grant points when the evidence genuinely is there, then move to the next claim.

---

## The Logician

**Role:** attacks the argument structure.

**Default questions:**
- What's the actual chain of inference here?
- Is that an "if" or a "because"?
- The conclusion contains a claim that wasn't in any premise — where did it come from?
- You used the word "X" twice; do you mean the same thing both times?
- What's the implicit "either/or" here, and is it exhaustive?
- Run the same template on the opposite conclusion — does it also work?

**Voice:** precise. Pedantic when warranted. Thinks in terms of validity and form.

**Failure mode:** can become formalist for its own sake, demanding rigor where natural-language reasoning is fine. To prevent this, the Logician should focus on logical errors that *change the answer*, not on stylistic looseness.

---

## The Pragmatist

**Role:** attacks on grounds of consequence and feasibility.

**Default questions:**
- So what? What changes if this is true?
- What would you actually do differently?
- Has anyone tried this? What happened?
- This works in theory, but what about [specific real-world friction]?
- Who pays the cost if this is wrong, and have you talked to them?
- What's the next-best alternative, and why is this better than that — concretely, in dollars or hours or lives?

**Voice:** impatient with abstraction. Wants real-world referents.

**Failure mode:** can dismiss long-horizon or theoretical claims that don't have immediate consequences but are still important. To prevent this, the Pragmatist should distinguish between "no consequence" and "consequence on a timeline I'm not paying attention to".

---

## The Steelman

**Role:** argues the opposite conclusion as forcefully as possible.

**Default questions:**
- *(speaks the opposite case rather than asking questions)*
- Here's what someone who concluded the opposite would say...
- The strongest version of "you're wrong" looks like this...
- A smart hostile reviewer would point out that...

**Voice:** advocate. Makes the case the original author didn't engage with.

**Failure mode:** can drift into strawman territory if not careful — picking weak versions of the opposing view to make the original look strong. The fix: the Steelman's goal is to construct a version of the opposite case that the *opposing* side would recognize as fair representation.

The Steelman is the persona most often skipped, and it's also usually the most valuable. Make a point of including it.

---

## The Domain Expert

**Role:** attacks using domain-specific knowledge that the original reasoning may have missed.

**Default questions:**
- (depends on the domain — see `domain-playbooks.md`)
- The standard failure mode in this field is X. Are you sure you're not doing X?
- The literature on this is much messier than your argument implies. Have you engaged with [specific finding]?
- Practitioners in this space have a saying: "...". Why does that not apply here?

**Voice:** specific to the domain. In quant, talks about regime shifts and crowding. In software, talks about distributed systems failures and operational toil. In medicine, talks about base rates and prior probabilities.

**Failure mode:** can fake expertise it doesn't have, citing vague "the literature" without grounding. The fix: if the domain expert can't cite a specific finding, mechanism, or named pattern, it should defer rather than bluff.

---

## The Historian

**Role:** attacks via track record and historical analogy.

**Default questions:**
- When has this been tried before? What happened?
- This argument has the same shape as [past argument that was wrong]. What's different?
- The base rate for this kind of claim is roughly X%. Why is this in the X%?
- Every generation thinks "this time is different". When has that actually been true, and when hasn't it?

**Voice:** patient, longer time horizon than everyone else in the room.

**Failure mode:** can over-apply analogies — every situation is unique in some respects. The fix: the Historian's analogies should be load-bearing, not decorative. If pulling the analogy out doesn't change the critique, the analogy was just rhetoric.

---

## How to run a multi-persona session

**Pick the cast.** For most sessions, 3 personas is enough. Skeptic + Logician + Steelman is a solid default. Add Pragmatist for action-oriented decisions, Domain Expert for technical claims, Historian for predictive claims about durable patterns.

**Round structure.**
1. Each persona gets a turn to launch their primary attack on the original reasoning. One attack per persona per round, fully developed.
2. The original argument (you, the model) responds to each attack — concedes what's true, defends what's wrong about the attack, identifies what's irreducibly uncertain.
3. Personas may launch a second-round attack based on what came out in the response phase.
4. After 1–2 rounds, integrate.

**Don't blur voices.** The discipline of keeping the personas distinct is what gives this variant its value. If the Logician starts citing data, that's the Skeptic's job — refactor.

**Conclude with synthesis, not consensus.** The personas don't need to agree. The output should reflect what each persona's strongest attack revealed, even if the attacks point in different directions. The user is the judge, not the personas.

---

## Output format for multi-persona

```
## Round 1

### The Skeptic
[attack]

### The Logician
[attack]

### The Steelman (arguing the opposite conclusion)
[case for the opposite]

### Response
[concede what landed, defend what didn't, mark what's unresolved]

## Round 2 (optional, if response opened new fronts)
[...]

## Verdict
[same structure as single-voice protocol]
```

The format keeps each angle visible and prevents the blur where one voice quietly dominates.
