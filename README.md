# Phalanx

Phalanx is an agentic orchestrator for work across repositories. Work items live in an API-backed queue. A local runner lets multiple agents claim work in parallel and deliver either a Markdown report or a pull request.

1. File a work item for a registered repository with a goal, enough context to execute it, and the expected output.
2. Start a **muster**, a batch of eligible work launched from the Captain chat. Agents work in parallel, use isolated checkouts for changes, and verify their results.
3. Follow each item through its lifecycle. PR work goes through development, self-review, live validation, PR creation, independent review, and a loop for comments and conflicts. The runner checks in the PR when authorized. Report work ends with a verified **field report** in Markdown. An agent can pause for a decision and resume after a reply.

The first milestone runs the HTTP API server and durable store locally, with concurrent work in visible Codex chats launched from a Captain chat. The runner has a configurable concurrency limit and can pause dispatch. The same server is intended to move to cloud hosting while agents continue to run locally. A future ChatGPT phone workflow can file work through the hosted API and return a work-item link. Claude Code and GitHub Copilot CLI are P2 execution targets.

See the [design spec](SPEC.md) for the proposed workflow and open decisions. Implementation has yet to start.
