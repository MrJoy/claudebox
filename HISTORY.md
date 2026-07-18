# History

## 0.0.3 - 2026-07-17

* Generalize the backend beyond Ollama Cloud: a new `PROVIDER` env var (`ollama` | `anthropic` | `custom`) selects the model provider. `ollama` remains the default, so existing configs are unchanged; `anthropic` targets Anthropic's own API, and `custom` targets any Anthropic-compatible endpoint. Every model tier is still pinned to the one `REVIEW_MODEL`.
* `PROVIDER=anthropic` no longer requires an `ANTHROPIC_API_KEY`: it falls back to `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or a mounted `~/.claude` credentials file, so you can reuse your existing `claude` login instead of a separate key.
* Add a `claudebox.sh` launcher that wraps `docker build`/`run` and the lifecycle commands (`build`, `run`, `test`, `logs`, `shell`, `stop`, `status`) with the required hardening flags baked in. Self-describing via `--help`; `--dry-run` prints the docker command without executing, and `--mount-claude` bind-mounts `~/.claude` for Anthropic login reuse.
* Move the provider-specific `REVIEW_MODEL` default out of the Dockerfile so each provider can supply its own (ollama: `glm-5.2:cloud`, anthropic: `claude-opus-4-8`).
* Tweak the default prompt to encourage a slightly bigger-picture perspective.

## 0.0.2 - 2026-07-01

* Add verbiage to prompts to allow addressing claudebox to force a re-review.
* Add verbiage to prompts to get claudebox to sign its comments.  This should make it a little easier to identify who's talking to who when Claude and Claudebox are both going through the same Github account.

## 0.0.1 - 2026-06-28

* Initial version with support for the Ollama cloud API, and default prompts tuned for glm-5.2.
