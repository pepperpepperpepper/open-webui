# Web search behaviour & runbook (Serper, native function-calling)

Goal: the model **decides** when to search and routes through our **external Serper
tool server** (clean snippets) — *not* OWUI's builtin search, and *not* a forced
search on every message.

If "it searches every message" or "it isn't using Serper" comes back, this is the
file to read first.

---

## The two search mechanisms (they are independent — don't confuse them)

1. **Builtin `search_web` / `fetch_url` tools** — `backend/open_webui/utils/tools.py`
   (`get_builtin_tools`, ~line 533-541). Injected into the native-FC tool list **only
   when `features['web_search']` is true** AND the model has the `web_search`
   capability AND `ENABLE_WEB_SEARCH` is on. Routes through OWUI's internal
   `process_web_search` path.

2. **External Serper tool server** — `deploy/serper-tool/server.py`, runs on
   `127.0.0.1:8093` under s6 (`open-webui-serper-tool`), `operation_id = web_search`.
   Registered in OWUI config under `tool_server.connections` (key = `SERPER_TOOL_TOKEN`).
   Attached to a chat via the tool/tool-server path — **independent of the
   `web_search` feature flag.** Returns raw top organic results + answer box.

The model can be handed *both* (`search_web` builtin vs `web_search` Serper). When the
builtin is present it tends to win → the symptom "uses the search tool, not Serper."

## The fork patch that makes it model-decided

`backend/open_webui/utils/middleware.py` (~line 2660-2667, applied via
`deploy/apply_local_patches.py`) **skips the forced built-in search** (and the
forced memory / image / code injection) when the model's
`params.function_calling == 'native'`:

```python
if 'web_search' in features and features['web_search']:
    # Skip forced RAG web search when native FC is enabled - model can use web_search tool
    if metadata.get('params', {}).get('function_calling') != 'native':
        form_data = await chat_web_search_handler(...)
```

So: **a model only gets the model-decided behaviour if `function_calling == 'native'`.**
A non-native model runs the forced RAG search every message (when the feature is on).

## The two things that force "search every message"

1. **User setting `webSearch: 'always'`** (`user.settings` JSON, top-level key) →
   `features['web_search'] = True` on every message (`middleware.py` ~2634). Combined
   with a non-native model = forced RAG search every turn; with a native model = builtin
   `search_web` handed over every turn.
2. **A model without `params.function_calling = 'native'`** → forced RAG path, can't use
   the Serper tool cleanly.

---

## Current good state (set 2026-06-14)

- All active models have `params.function_calling = 'native'`
  (`speciale`, `cerebras.zai-glm-4.6/4.7`, `cerebras.gpt-oss-120b`, `cerebras.llama3.1-8b`).
- The primary user's `webSearch: 'always'` flag was **removed**.
- Builtin web search config: `engine = serper`, `bypass_embedding_and_retrieval = true`,
  `bypass_web_loader = true` (so even the builtin path returns clean snippets).

## Runbook — if web search misbehaves again

DB: `psql 'postgresql://open_webui@127.0.0.1:5432/open_webui'`
(`model.params` and `model.meta` are **TEXT** columns holding JSON; `user.settings` is
a **json** column.)

1. **Make every tool-capable model native-FC** (merge-safe; preserves other params):
   ```sql
   UPDATE model
      SET params = (params::jsonb || '{"function_calling":"native"}'::jsonb)::text
    WHERE is_active;
   ```
2. **Drop the forced "always search" flag** on the account that's misbehaving:
   ```sql
   UPDATE "user"
      SET settings = (settings::jsonb - 'webSearch')::json
    WHERE email = '<email>';
   ```
3. **(Optional, bulletproof)** make Serper the *only* search tool even if the chat's
   Web Search toggle gets hit — disable the builtin web_search category per model:
   ```sql
   UPDATE model
      SET meta = jsonb_set((meta::jsonb), '{builtinTools,web_search}', 'false', true)::text
    WHERE is_active;
   ```
   (`is_builtin_tool_enabled('web_search')` then returns false → builtin `search_web` /
   `fetch_url` are never injected.)
4. **Reload model params** — `app.state.MODELS` is cached, so restart the service:
   ```sh
   sudo s6-svc -r /service/open-webui
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/health   # expect 200
   ```
   (The `user.settings` change is read per-request — no restart needed for that.)

## Verify

- Confirm the Serper tool server is up: `curl -s http://127.0.0.1:8093/health`
  → `{"ok":true,"configured":true}`.
- Watch live tool calls: `tail -f /home/arch/logs/open-webui-serper-tool/current | grep POST`.
- In a fresh chat: a non-web question → **no** `POST /search`; a current-info question →
  exactly one `POST /search`.

## Gotchas

- Manually clicking the chat **Web Search toggle** re-enables `features['web_search']` for
  that chat → builtin `search_web` returns (unless step 3 is applied). With model-decided
  Serper you shouldn't need the toggle.
- The middleware skip relies on the fork patch in `apply_local_patches.py`; after an
  upstream merge confirm that patch still applies (`reapply_after_open_webui_upgrade.sh`).
- New models added later default to non-native FC — re-run step 1.
- Secrets (`SERPER_API_KEY`, `SERPER_TOOL_TOKEN`) live in `deploy/serper-tool/serper-tool.env`
  (gitignored). The same Serper key is also used by the LiveKit agent (`deploy/livekit/`).
