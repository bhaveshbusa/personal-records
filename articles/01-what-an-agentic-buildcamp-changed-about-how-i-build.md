---
title: "What an Agentic Buildcamp Changed About How I Build"
subtitle: "Building an AI-powered personal-records system taught me that speed matters—but trustworthy boundaries matter more."
status: draft
---

I joined Agentic Buildcamp wanting to become faster at turning ideas into working software.

That happened. I learned a much more agentic way of building: moving between a rough product idea, a working prototype, a test case, an architectural question, and back again—without treating each step as a separate hand-off. The pace can be exhilarating.

But the more durable lesson was not that AI makes it possible to ship more. It changed what I think is worth designing carefully.

I came away with a stronger belief that good AI products are not defined by how much the model can do. They are defined by the boundaries around what the model is allowed to influence, what the system must verify, and where a person remains responsible for the outcome.

I explored that question through a small project called Personal Records. It is a local-first system for the moments when paperwork suddenly matters: an insurance renewal looks expensive, someone needs proof of cover, or a comparison site asks for facts spread across years of documents.

The project is deliberately modest. It is not a polished consumer app, and it does not pretend to solve every household-admin problem. But building it changed how I think about four things.

## Start with a consequential moment, not a technical capability

It is tempting to start an AI project with what the model can do: read PDFs, extract data, answer questions, call tools. Those are useful capabilities, but they are not the reason anyone needs a product.

People do not wake up wanting to ingest an insurance document. They need to decide whether to renew, work out what they are covered for after an accident, prove their no-claims history, or avoid an uninsured gap.

That shift sounds small, but it changes the whole design. Instead of asking, “How do I build document extraction?”, I started asking, “What would help someone make a decision at the exact moment this paperwork becomes important?”

The answer was not one feature. A renewal comparison needs trusted facts, a sense of time, original evidence, and a clear explanation of what changed. A coverage question needs the relevant policy wording and an exact citation. Re-quoting needs a profile assembled from documents that were never designed to work together.

The product’s front door is therefore a moment of need. The documents are evidence that can help resolve it.

That is a change I want to keep: begin with the decision or outcome, then work backwards to the capabilities it requires.

## Treat AI output as a proposal

The project gave me a very concrete reason to stop thinking of structured model output as an answer.

One of the example documents was an insurance renewal invitation covering both a motor policy and a home policy. The model interpreted it as a single motor renewal and put the bundle total into the motor-premium field. The output was well structured. It had a high confidence score. It even included the source text that supported the amount.

It was still wrong.

Downstream, the application calculated a 62.7% price increase. That number looked precise and alarming. It was also based on a category mistake: a combined total is not the premium for one product line.

My first instinct was familiar: perhaps the prompt needed tightening, or the JSON schema needed another field. Neither was the real fix. The problem was that the application had not represented the document’s domain shape clearly enough.

The system now treats a model’s reading of a document as a proposal. Deterministic checks decide whether that proposal may become accepted state. They check things such as whether the document has one product line or several, whether totals reconcile, whether dates and links make sense, and whether a renewal is proposed or has already been accepted.

When the shape is ambiguous, the system does not quietly choose the most plausible story. It creates a review item and records no facts automatically.

That is not an admission that the AI failed. It is the product behaving honestly about uncertainty.

## Make the important states explicit

The Buildcamp also made me more alert to the difference between an interface that appears to work and a system that can explain what it believes.

In Personal Records, the original document is immutable evidence. An accepted fact is a domain event. The current list of policies or renewals is a projection that can be rebuilt from those events. Those may sound like implementation details, but they let the system answer important questions:

- What did the original document actually say?
- Was this value extracted automatically or confirmed by a person?
- Why does the system currently consider this policy active?
- What changes if a document is later found to be wrong or irrelevant?

This structure also makes it possible to retract facts from a document without deleting the document or rewriting history. The distinction is valuable: evidence can exist without being trusted, and something once trusted can later be corrected.

I used to be more willing to let a convenient data structure stand in for a product model. Now I am more likely to pause and ask which states are genuinely different, which transitions matter, and what proof the system should retain.

## Use agents to increase judgement, not remove it

Agentic development did make me faster. It helped explore options, write initial implementations, turn a failure into a regression test, and keep momentum through unfamiliar terrain.

But it also made a counterintuitive point clearer: when the cost of producing software falls, judgement becomes more important, not less.

It becomes easier to add a tool, expose a write operation, or make a model seem autonomous. It also becomes easier to ship an assumption that nobody has examined closely. The useful question is not “Can an agent do this?” It is “What is the safest useful role for it here?”

For this project, that means an assistant can investigate records, retrieve evidence, and explain what it found. It cannot silently approve an extraction, rewrite a policy, or append durable events because a conversation sounded convincing.

The model can be dynamic about reading messy documents and interpreting human questions. The application remains strict about facts, calculations, permissions, and state changes.

That division of labour feels like a better definition of agentic than simply giving a model more tools.

## Build the learning loop into the work

The most useful artefact in the project is not the happy-path demo. It is the failure case: the multi-policy renewal that must never silently become a single-policy quote.

Keeping that example in the test suite changes the nature of the lesson. It is no longer an anecdote about something that went wrong once. It is a constraint that future changes have to respect.

That is another practice I want to carry forward. When an AI system surprises us, the response should not end with changing a prompt and hoping. We should decide what the failure revealed about the product model, make the safer behaviour observable, and preserve the case as an evaluation.

## What I am taking forward

I finished the Buildcamp more optimistic about building with AI, but less interested in treating intelligence as a substitute for design.

I want to build systems that begin with real moments of need; use models to interpret ambiguity; make evidence, authority, and uncertainty visible; and remain useful even when they cannot safely act.

Personal Records is only an early case study. The more interesting question is what comes next when a system can notice a meaningful situation, investigate it within clear limits, and help a person carry it to a decision.

That is the direction I want to explore next.

