# Configuration chat API

Control Plane uses the Gateway JSON API to show worker observations and edit managed configuration through conversation. No business project or task is created for a conversation. Worker activity remains the observed `/v1/workers` report; an assistant reply is not evidence that a production worker is running a job.

## Routes

All routes are under `/v1/config/chat`. Gateway authentication is always required. Session creation, messages and proposal acceptance additionally require `X-Config-Admin-Token`. Session creation and messages require `Idempotency-Key`.

- `GET /sessions`, `POST /sessions` (`source_id`, `base_sha256`). Only profile, skill and harness text is supported.
- `GET /sessions/{id}` returns the durable transcript, revision, latest proposed content and diffs against the installed base.
- `POST /sessions/{id}/messages` (`expected_revision`, `message`) queues one immutable model turn. One pending turn per conversation, four globally, at most 40 turns per conversation.
- `POST /sessions/{id}/accept` (`expected_revision`, `turn_id`) accepts only the latest completed proposal, checks the installed base, saves a draft and runs basic validation. Repeated acceptance returns the same draft.
- Accepted drafts use the existing `/v1/config/drafts/{id}/release` workflow. CI, isolated runtime trial and deployment observation remain required.

The controller-only `GET /controller/work` and `POST /controller/turns/{id}/report` require `X-Config-Controller-Token`. Turns are bound to a hash of their immutable context. Pending turns expire after 15 minutes when the controller polls. Terminal reports cannot revive failed or expired turns.

## Model runtime

The existing configuration controller runs deterministic, isolated Kubernetes Jobs in its existing trial namespace. Jobs have no service-account token, provider credentials, GitHub access or direct Internet route. They send only the selected configuration, latest proposed content and bounded recent conversation to the existing text-only Responses broker. Tools are disabled by the broker. Full conversation history stays in Gateway; the latest ten replies (bounded excerpts) accompany each model request.

`chat_model` in the operator-owned trial settings selects the model (default `gpt-5.6-sol`). Model output must be a completed, bounded JSON reply with optional whole-file proposal. Truncated streams, malformed replies and runtime failures are reported as failures. There are no automatic model retries within a job. Failed turns can be explicitly resent. Chat Jobs and their input ConfigMaps expire after one hour.

Deploy the Gateway schema/API first, then update the controller files listed in `deploy/install.py`, then deploy Control Plane. Existing controller credentials, broker credentials, namespace RBAC and network policies are reused without broader permissions. Conversation records are additive PostgreSQL tables; rolling back the application does not require dropping them.

Protocol reference: [Responses streaming events](https://developers.openai.com/api/docs/guides/streaming-responses).
