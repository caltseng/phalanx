# Phalanx

Phalanx is an agentic orchestrator for work across repositories. Work items live in an API-backed queue. A local runner lets multiple agents claim work in parallel and deliver either a Markdown report or a pull request.

1. File a work item for a registered repository with a goal, enough context to execute it, and the expected output.
2. Start a **muster**, a batch of eligible work launched from the Captain chat. Agents work in parallel, use isolated checkouts for changes, and verify their results.
3. Follow each item through its lifecycle. PR work goes through development, self-review, pre-PR validation, PR creation, CI and preview validation, independent review, and a loop for comments and conflicts. The runner checks in the PR when authorized. Report work ends with a verified **field report** in Markdown. An agent can pause for a decision and resume after a reply.

**Phalanx advancing** means a work item has met the conditions to move to its next stage.

The first milestone runs the HTTP API server and durable store locally, with concurrent work in visible Codex chats launched from a Captain chat. The runner has a configurable concurrency limit and can pause dispatch. The same server is intended to move to cloud hosting while agents continue to run locally. A future ChatGPT phone workflow can file work through the hosted API and return a work-item link. Claude Code and GitHub Copilot CLI are P2 execution targets.

See the [design spec](SPEC.md) for the proposed workflow and open decisions. Open [state-machine.html](state-machine.html) locally in a browser to explore the lifecycle and execution status graphs. Implementation has yet to start.

Every closed work item has a final output. If it ends without the requested deliverable, Phalanx stores a field report explaining what happened and what remains.
