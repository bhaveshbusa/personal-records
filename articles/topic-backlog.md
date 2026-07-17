# Article topic backlog

These are intentionally short briefs, rather than outlines. They preserve the
reason each topic is interesting while leaving room to write from the next
iteration of the product.

## From records to decisions

**Possible titles**

- *From Stored Records to Better Decisions*
- *The Product Is Not the Document Vault*

**Central idea**

Personal records become valuable at a decision point—renewing, claiming,
proving, comparing, or disputing—not when they are merely uploaded. Explain how
a fact store with provenance, time, wording retrieval, and evidence supports
several different high-stakes jobs.

**Why write it**

This is the clearest product-strategy piece in the series and works for readers
who do not care about the technical architecture.

## Designing human-in-the-loop automation

**Possible titles**

- *Human in the Loop Is a Product Design, Not a Fallback*
- *Where AI Should Pause and Ask*

**Central idea**

Explore confidence thresholds, review queues, source evidence, explicit
confirmation, and correction. Make the argument that good automation does not
remove people from every step; it routes human attention to the uncertain or
consequential ones.

**Useful example**

The multi-policy renewal: confidence and provenance were present, but domain
shape was unsafe, so the correct outcome was a review item and zero automatic
events.

## A case loop, not a chatbot

**Possible titles**

- *Why a Case Loop Beats a Chatbot for Consequential Work*
- *The Conversation Is Not the System of Record*

**Central idea**

Take one element of the future-direction article and go deeper. Contrast
one-off chat with a resumable case that retains evidence, tool results,
questions, approvals, decisions, and terminal states.

**Key claim**

A useful system must be able to pause, resume, explain its current state, and
end as `unsupported_transition` when no safe action is available.

## Building trustworthy AI workflows

**Possible titles**

- *Trustworthy AI Workflows Need More Than Guardrails*
- *Evidence, Authority, and the Right to Say “I Don’t Know”*

**Central idea**

Write a cross-domain framework for provenance, explicit uncertainty, least
authority, exact approvals, idempotency, audit, and fail-closed behaviour. This
could apply equally to financial, healthcare, compliance, or operations tools.

**Audience**

Product leaders and engineering teams trying to move from an impressive AI demo
to software people can rely on.

## The capability register as product architecture

**Possible titles**

- *A Capability Register Is an API for Authority*
- *What Your Agent Is Actually Allowed to Do*

**Central idea**

Go deeper on the capability register as a closed catalogue of meaningful domain
reads and commands. Distinguish domain capabilities—such as replacing a current
policy—from generic storage writes such as `update_record` or `append_event`.

**Why it is distinct from article three**

The future-direction article introduces the register as one architectural
component. This article can make its design, versioning, preconditions,
approvals, and audit implications concrete.

## Designing for useful refusal

**Possible titles**

- *The Most Trustworthy Thing an Agent Can Do Is Stop*
- *Useful Refusal Is a Feature*

**Central idea**

Investigate the difference between an agent failing, refusing, and successfully
terminating an unsupported transition. Show how a system can remain useful by
gathering evidence, explaining uncertainty, and identifying the missing
authority without inventing an action.

**Why it may resonate**

It gives a practical alternative to the pressure for ever more autonomous
agents, and it has a memorable central claim.

## The evaluation ladder for AI systems

**Possible titles**

- *What I Learned by Testing the Failure, Not the Demo*
- *An Evaluation Ladder for Evidence-Backed AI Systems*

**Central idea**

Explain why prompt examples are not enough. Walk through evaluations for
classification, extraction, domain shape, routing, projections, retrieval,
authority, and unknown-case behaviour. Use the preserved multi-policy renewal
as the recurring example.

**When to write it**

After more live-model evaluation data exists; it will be stronger with a few
measured results and a second or third failure case.

