# Phalanx

Phalanx is an agentic orchestrator for work across repositories. Work items live in an API-backed queue. A local runner lets multiple agents claim work in parallel and deliver either a Markdown report or a pull request.

1. File a work item for a registered repository with a goal, enough context to execute it, and the expected output.
2. Run Phalanx locally. Available agents claim eligible items, use isolated checkouts for changes, and verify their results.
3. Get a link to the report or pull request. If an agent needs a decision or hits a blocker it cannot resolve, it asks for input and resumes after a reply.

The first milestone is a working API and local runner. A future ChatGPT phone workflow should be able to file work into the same queue and return a work-item link.

See the [design spec](SPEC.md) for the proposed workflow and open decisions. Implementation has yet to start.
