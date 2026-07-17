---
title: "DDD Meets LLM Ambiguity: Designing for Evidence, Not Guesswork"
subtitle: "Why structured output, confidence, and provenance are not enough—and how domain boundaries make AI systems safer."
status: draft
---

An LLM extracted an insurance premium with high confidence, correct source evidence, and valid structured output. The system then calculated a 62.7% renewal increase.

Every technical signal looked reassuring. Every product conclusion was wrong.

The document was a combined motor-and-home renewal invitation. The model had interpreted it as a single motor-policy renewal and put the bundle total into the motor premium field. Nothing had been fabricated. The amount was really on the page. The JSON matched the schema. The model was still describing the wrong thing.

That failure is a useful way to understand the relationship between domain-driven design and LLM-powered systems. The problem was not an insufficiently clever prompt. It was that the domain model did not express an important distinction: a document-level total is not the same as a product-line premium.

I built Personal Records to explore this boundary. It is a local-first system that turns unstructured personal documents into evidence-backed records and answers. The central principle is simple:

> AI proposes. The system verifies. A human resolves uncertainty.

That is not a slogan for making an LLM less useful. It is what makes it useful in a domain where confident mistakes can quietly contaminate future decisions.

## The hidden risk of valid output

Most work on reliable LLM extraction focuses on recognisable problems: invalid JSON, missing fields, fabricated values, or poorly calibrated confidence.

Those matter. But the harder failures are often semantic.

In the renewal example, the model returned a number from the source document and labelled it correctly according to the schema it had been given. The schema allowed `annual_premium`. It did not adequately distinguish between a premium belonging to one policy line and a stated total belonging to a document that covered several lines.

This is why structured output is necessary but not sufficient. It ensures that an answer fits a shape. It does not ensure that the shape is the right model of reality.

Confidence does not solve the problem either. Confidence answers a narrow question: how certain was the model about its extraction? It does not answer whether the application has interpreted the document correctly.

Provenance is equally important but incomplete. A source snippet tells us where a value came from. It cannot establish that the value has the business meaning we assigned to it.

The distinction can be stated plainly:

| Signal | What it can tell us | What it cannot tell us |
|---|---|---|
| Structured output | The result conforms to a declared format | The format models the situation correctly |
| Confidence | The model is certain about its interpretation | The interpretation belongs to the right domain concept |
| Provenance | A value is grounded in source evidence | The value has been attached to the right entity or relationship |

The remedy is not to discard these signals. It is to connect them to domain concepts and deterministic authority.

## Start with the domain, not the document parser

The original version of the system made a familiar assumption: a renewal document maps neatly to a policy. That assumption is often true, which is exactly why it is dangerous.

The failure forced several domain concepts into the open:

- A document can describe one or more product lines.
- A document-level stated total is separate from the premium of each product line.
- A renewal proposal is different from a renewal that has already been accepted.
- The identity of a source document is different from the identity of the policy or vehicle it refers to.
- An unrecognised or ambiguous document is a valid outcome, not an extraction error to be hidden.

These are not abstractions added to impress an architect. They are the language required to stop an apparently sensible value from becoming a harmful fact.

This is where DDD is particularly helpful. It asks us to identify the distinctions that matter in the domain and make them explicit in the model, the language, and the invariants. LLMs make this work more—not less—important because they can produce fluent answers even when the underlying distinctions are blurred.

## Let the model interpret; let the domain decide

An LLM is well suited to the messy edge of the system. It can read varied layouts, classify an unfamiliar document, identify candidate values, retrieve supporting snippets, and interpret a human question.

It should not be the authority that decides which proposed facts become durable state.

In Personal Records, extraction creates a typed proposal. Every proposed value carries the extracted value, a confidence score, a source snippet, and page information where available. But proposals are not facts.

Before any event is appended, deterministic validation checks the proposal against the domain model. Among other things, it asks:

- Is the document type known and sufficiently confident?
- Does the document have a safe shape, or is it multi-line or unsure?
- Are required values present and credible?
- Do extracted line premiums reconcile with a declared document total?
- Is this a new renewal proposal or an already-accepted renewal?
- Can the document be linked unambiguously to the relevant policy or vehicle?
- Would acceptance overwrite incompatible current evidence?

If the answer is unsafe or incomplete, the system emits zero automatic events. The document stays in the evidence store, and a review item explains why it needs a decision.

That last point matters. A review queue is not merely an operational fallback. It is a domain outcome: the system has evidence but is not entitled to turn it into accepted truth.

## Evidence, events, and projections solve different problems

A second useful DDD distinction is between evidence, accepted facts, and current views.

The document itself is immutable evidence. It has a content-based identity, so the system can establish which exact source was processed and avoid treating an identical re-upload as new information.

Accepted facts and decisions are captured as typed domain events: a policy was filed, a renewal was proposed, a renewal was accepted, or a no-claims record was confirmed. An event records that the application was authorised to believe something at a point in time. It is not a mutable row that quietly replaces the past.

The current policy list, renewal calendar, and quote comparison are projections built from those events. They can be rebuilt. When a document is later shown to be irrelevant or wrong, a retraction can change the projection without deleting the evidence or pretending the earlier decision never happened.

This separation gives the system a more truthful vocabulary:

- “This document exists.”
- “The extractor proposed this fact from it.”
- “The system accepted this fact under these rules.”
- “This is what the system currently concludes.”

Those statements are easy to collapse in a conventional CRUD application. With AI in the loop, keeping them separate is a practical defence against invisible corruption.

## Prefer a visible gap to a confident misfiling

The design principle that emerged most strongly is this: misfiled is worse than unextracted.

If the system cannot classify a document, that is inconvenient but recoverable. If it assigns the right premium to the wrong policy, period, or product line, every later answer can become plausibly wrong.

That is why the system fails closed at the authority boundary. Unknown document types, ambiguous entity links, unsafe shapes, conflicting evidence, and questionable totals do not get “best guessed” into the record. They become visible work for a person.

This does not mean users must manually inspect every document. Straightforward, evidence-backed cases can still be accepted automatically. The point is to reserve automation for cases where the domain rules give the application a real basis for confidence.

The aim is not maximum automation. It is maximum trustworthy progress.

## Domain commands, not generic writes

The same principle applies once an assistant can do more than read.

It is tempting to expose generic operations such as `update_record`, `write_json`, or `append_event` to an agent. Those are storage operations, not domain capabilities. They bypass the very invariants that make the records trustworthy.

If the system needs to replace a current policy, it should expose a domain command such as `replace_current_policy`. That command can require exact evidence, check that the two policies refer to the same entity, verify relevant dates, require an explicit approval, and append the appropriate event exactly once.

The difference may feel pedantic until a conversational interface says “yes” to a subtly different action than the user intended, or a retry causes an effect twice. DDD supplies the right boundary: the agent may propose a meaningful domain action, but only the application can authorise and execute it.

## Turn failures into executable learning

The multi-policy renewal is now a regression case. The system must route it to review and must append no renewal event automatically.

That is how an LLM failure becomes a product improvement rather than a prompt anecdote. The incident tells us what was missing in the model. The new concept becomes a validation rule. The case becomes an evaluation that future versions must pass.

For AI systems, evaluation needs to be layered accordingly. We should test not only whether extraction is accurate, but whether the system correctly understands shape, routes uncertainty, rebuilds state, retrieves evidence, and refuses unsafe actions.

## DDD is the companion to probabilistic software

LLMs are not a reason to abandon domain modelling. They are a reason to be more serious about it.

The model can help with what conventional software is poor at: reading ambiguous language, interpreting varied documents, and engaging in a useful conversation. Deterministic domain code remains better at identities, invariants, calculations, authority, replay, and audit.

The important design move is to make that hand-off explicit. AI can propose a reading of the world. The domain decides what the system is allowed to believe and do.

When we get that right, ambiguity is not eliminated. It is given an honest place to go.

