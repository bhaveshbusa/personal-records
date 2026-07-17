## Jobs

**1. Renewal arrives** — _"Help me decide: renew, negotiate, or switch — without overpaying."_ The loyalty-penalty job. Needs year-over-year premium history, what changed in cover, and a comparison-ready summary of my current deal.

**2. Just had an accident** — _"Tell me right now: what am I covered for, what's my excess, who do I call?"_ Rare but highest-stakes. Also: capture evidence at the scene (photos, third-party details) and attach it to the policy record.

**3. Someone demands proof** — _"Give me my certificate of insurance / NCD proof, now."_ Police stop, rental counter, border crossing, or a new insurer demanding no-claims proof from the old one (a classic switching pain).

**4. "Am I covered for X?"** — _"Answer a coverage question in plain language, with the clause cited."_ Driving someone else's car, commuting vs. social use, driving abroad, towing, lending the car, business use. People never read the policy wording; they need it interrogated on demand.

**5. Life changes** — _"I moved / changed jobs / modified the car / added my kid — what must I tell the insurer before my policy becomes void?"_ Material-fact obligations are invisible until a claim gets rejected. The system knows my declared facts and flags divergence.

**6. Staying legal, always** — _"Never let me be accidentally uninsured."_ UK-specific bite: Continuous Insurance Enforcement means a registered car must be insured or SORN'd. Gap detection across renewal dates, plus natural adjacency to MOT and road tax.

**7. Re-quoting anywhere** — _"Give me my quote-ready profile."_ Comparison sites ask for facts scattered across years of documents: NCD years, claims in last 5 years, convictions, mileage, overnight parking. A normalized fact sheet kills 30 minutes of form-filling.

**8. Household admin** — _"One place for everyone's cars, policies, named drivers, and dates."_ Multi-car, spouse, young drivers. The buyer is often the household admin, not the individual policyholder.

**9. Disputing the insurer** — _"Help me challenge a rejected claim or unexplained hike with exact wording and my evidence trail."_ Needs versioned documents, correspondence log, provenance.

**10. Understanding what I bought** — _"What add-ons am I paying for, and am I double-covered?"_ Legal cover, key cover, breakdown — often duplicated via bank accounts or other policies. Cross-record overlap detection.


## Working backwards to design

Plot these on **frequency × stakes**: renewal (annual, high £), accident (rare, extreme), coverage Q&A (occasional, medium), proof retrieval (occasional, urgent). The interesting insight: almost every job decomposes into the same five capabilities —

- **Normalized fact store with provenance** → jobs 1, 3, 4, 5, 7, 9
- **Time/event model** (what was true when) → 1, 5, 6, 9, 10
- **Cited Q&A over policy wording** → 2, 4, 10
- **Calendar + action layer** → 1, 6
- **Evidence vault** (docs, photos, correspondence) → 2, 3, 9

So the JTBD lens validates the fact-store-with-provenance core, but reframes the _front door_: people don't hire this to "ingest documents" — they hire it at renewal, at the accident scene, or when filling a quote form.