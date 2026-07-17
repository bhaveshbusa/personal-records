---
title: "From Records to Cases: A Safer Direction for Agentic Systems"
subtitle: "Attention layers, capability registers, playbooks, and bounded loops for systems that help without quietly acquiring authority."
status: draft
---

Most personal-information systems are passive. They wait for us to remember that something matters, find the right document, understand it, decide what to do, and then carry out the next step.

That is the limitation I am exploring beyond Personal Records. A document vault can retain evidence. A good search experience can retrieve it. But neither notices that a renewal is approaching without a quote, that two policy periods may overlap, or that a document is awaiting a decision before it can safely inform the record.

The obvious response is to “add an agent.” Give it access to tools, ask it to monitor the data, and let it act on a user's behalf.

I think that is the wrong architectural starting point.

The future I find more compelling is a bounded case system: one that notices meaningful situations, investigates them with explicitly permitted reads, asks for clarification when needed, proposes an exact action, and either obtains informed approval or safely stops.

The components I am exploring are an Attention Layer, a Capability Register, Playbooks, and a bounded case loop. Together, they offer a direction for making systems more proactive without making them unaccountable.

## People have cases, not files

Consider the moments when insurance paperwork matters:

- A renewal arrives and the price has changed.
- An accident happens and someone needs the policy details, excess, and evidence immediately.
- A comparison site asks for years of no-claims, mileage, and vehicle details.
- A person moves house, changes jobs, or adds a driver and needs to know what must be disclosed.
- A current policy is about to lapse.

None of these are fundamentally document-management jobs. They are cases: a situation with evidence, uncertainty, a possible outcome, and sometimes a consequential decision.

The same idea applies outside insurance. Financial records, health documents, compliance evidence, and operational workflows all contain moments where information should become attention—not merely search results.

The question is how to make that transition safely.

## The Attention Layer: turn evidence-backed gaps into work

An Attention Layer sits between the system's current projections and the user interface. It looks for deterministic, evidence-backed conditions that deserve attention.

For Personal Records, examples might include:

- A new schedule may replace or overlap the policy currently considered active.
- A renewal date is approaching but there is no comparison-ready quote.
- A current policy has no associated policy wording.
- An entity link is ambiguous.
- A required document is waiting for review.

An attention item is not a fact about the outside world. It is operational state: a well-defined reason for the system to ask for a decision or further information.

That distinction is important. The item should point to the exact evidence and record versions that caused it, state its rationale, expose its risk, and list the allowed resolutions. If the evidence changes, the old attention item should not remain an invisible authorisation for a new action.

For example, a possible policy replacement might carry the current-policy document ID, the candidate document ID, the relevant entity, and the fact that the periods overlap or are adjacent. Its resolutions might be to replace the current policy, keep both as concurrent, relink the candidate, or reject it.

Attention is how a passive record system begins to be helpful. But by itself it should not grant an agent any new power.

## The Capability Register: define authority as data

An agent with a list of tools knows what functions exist. It does not necessarily know what it is authorised to do, which inputs are safe, or what proof is required.

A Capability Register fills that gap. It is a closed catalogue of reviewed reads and commands. Each capability describes not just its name and input schema, but the constraints that make it legitimate:

- whether it is a read or a command;
- its version and input/output schema;
- deterministic preconditions;
- risk level;
- approval policy;
- exact evidence-binding requirements;
- idempotency identity;
- durable effects and audit information;
- possible failure outcomes.

This is more than API documentation. It makes authority machine-readable and enforceable.

For example, `replace_current_policy` is a meaningful domain command. It can require an open attention item, bind the current and candidate policy documents by exact identifiers, verify that the entities match, check that the periods are relevant, require explicit approval, and execute only once.

Compare that with a generic tool called `append_event` or `update_record`. Those tools may be convenient for an implementation, but they bypass the domain concepts that protect the user. They are storage primitives, not capabilities a conversational system should receive.

The principle is simple: the model may choose among reviewed capabilities. It may not invent a new capability, weaken a precondition, or reinterpret a read as permission to write.

## Playbooks: a middle ground between scripts and autonomy

Every meaningful case needs some structure. A hard-coded workflow for every situation is brittle and expensive. An unconstrained agent loop is difficult to trust and nearly impossible to evaluate.

Playbooks provide a middle ground.

A playbook defines how registered capabilities may be combined for a recognised type of case. For a possible policy replacement, it could specify the deterministic trigger conditions, the relevant read capabilities, permitted commands, clarification questions, and terminal states.

Within that boundary, the model can still be useful. It can decide which relevant records to inspect first, avoid asking a question that the evidence already answers, explain the trade-off in everyday language, and choose when it has enough information to formulate a proposal.

But it cannot escape the case by calling unrelated tools or producing a novel mutation because it feels that the conversation has reached a conclusion.

Playbooks are not prompts. They are executable policy around a class of cases.

They also create a useful route for learning. If repeated cases reach an unsupported transition, the system can preserve the evidence and the point of failure. A model may help draft a possible playbook, but a developer must review the new domain transition, implement any required command and event, and add evaluations before it becomes live.

In other words, the system can learn where it lacks a capability without granting itself one.

## The bounded case loop

These pieces come together in a small, resumable loop:

1. A document or detector opens an attention item.
2. The system binds the exact evidence versions and the playbook's allowed capabilities.
3. The model investigates using registered reads.
4. It asks a concise clarification question if evidence alone is insufficient.
5. If a registered command fits, it creates an exact proposal: action, inputs, evidence, and consequences.
6. The user explicitly approves that proposal.
7. The server revalidates the evidence and preconditions, then executes the idempotent command.
8. If no registered command fits, the case ends as `unsupported_transition`—with the findings preserved.

The model is not an omniscient controller in this design. It is a participant in an accountable process.

The loop should also be bounded. Calls and retries have limits. Pausing for a human response is a normal state, not an error. Every tool result, clarification, approval, and outcome can be persisted so a case can resume without reconstructing its reasoning from a fragile chat transcript.

## “Unsupported” can be a successful outcome

One of the hardest habits to unlearn in agentic design is the assumption that a system must finish the job once it begins.

In a consequential domain, there are cases the system should investigate but not resolve autonomously. Perhaps two documents cannot be linked with enough certainty. Perhaps the correct action does not yet have a registered command. Perhaps the user has not supplied a required fact.

The system should still provide value: gather the relevant evidence, explain the ambiguity, ask the smallest useful question, and make clear what it cannot safely do.

Then it should stop.

`unsupported_transition` is not a failure to be hidden. It is an honest result: the system has reached the boundary of its authority. That boundary is what makes its supported actions more credible.

## Approval has to bind to the exact change

Natural-language consent is not enough for a consequential write. A message saying “yes, do it” can become ambiguous if the evidence changes, a retry occurs, or the assistant's proposal was underspecified.

The approval protocol must bind the person's decision to exact current inputs and consequences:

1. The assistant investigates through read capabilities.
2. It presents a proposal with the precise command, arguments, evidence versions, and effects.
3. The user explicitly approves that proposal.
4. The server checks that the proposal is still current and has not expired.
5. The command handler reruns authoritative preconditions.
6. The idempotent command runs once, and the decision is recorded.

This is not friction for its own sake. It prevents an approval for one policy document from being silently reused after that document changes, or a conversation from becoming a substitute for a verifiable state transition.

## Agentic systems need a theory of authority

The attention layer, capability register, playbooks, and bounded case loop are not features to bolt on after adding an agent. They are a theory of authority for software that uses agents.

They preserve a productive division of labour. The model can be flexible in investigation and conversation. Deterministic software stays responsible for identities, state, permissions, preconditions, and audit. The human remains the decision-maker for consequential changes.

This is still a design direction, not a claim that every hard problem has been solved. Important questions remain: which attention detectors must be deterministic, how declarative playbooks can remain before they become a programming language, how approvals work across different assistant hosts, and how long-running cases reconcile concurrent changes.

But the direction feels more promising than an all-powerful assistant with a broad tool belt.

The most useful agentic systems may not be the ones that can do anything. They may be the ones that know exactly what they are allowed to do, can show why, and can stop safely when the answer is not yet theirs to make.

