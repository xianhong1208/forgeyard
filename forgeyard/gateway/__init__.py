"""The gateway (M4): one OpenAI-compatible API at /v1 in front of every model deployment.

- `keys`: API keys (random, shown once, stored as a SHA-256 with a visible prefix), their
  model allowlist and limits.
- `limits`: requests and tokens per minute per key (in this process).
- `balancer`: least in-flight requests per replica, ties taken in turn.
- `proxy`: forwarding, with failover before the first byte and streamed answers passed through.
- `usage`: counters per day, key, user and model, written to the database every few seconds.
- `multipart`: just enough multipart/form-data to rewrite a transcription request's model.
"""
