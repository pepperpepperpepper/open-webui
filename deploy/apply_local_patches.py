#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


CALL_OVERLAY_SOURCE_SUFFIX = "src/lib/components/chat/MessageInput/CallOverlay.svelte"
CHAT_CONTROLS_SOURCE_SUFFIX = "src/lib/components/chat/ChatControls.svelte"
CARTESIA_HOST_SNIPPET = "cartesia.ai"
PROVIDER_TIMING_MARKER = "OWUI_LOCAL_PATCH_PROVIDER_TIMING"


def _contains_bytes(path: Path, needle: bytes, *, chunk_size: int = 1024 * 1024) -> bool:
	# Sourcemaps can start with a very large "mappings" string, so we can’t just read a prefix.
	# Do a streaming substring search to avoid loading huge files into memory.
	if not needle:
		return True

	overlap = max(0, len(needle) - 1)
	tail = b""

	with path.open("rb") as f:
		for chunk in iter(lambda: f.read(chunk_size), b""):
			data = tail + chunk
			if needle in data:
				return True
			tail = data[-overlap:] if overlap else b""

	return False


def _find_frontend_root() -> Path:
	import open_webui  # noqa: F401

	package_dir = Path(open_webui.__file__).resolve().parent
	frontend_root = package_dir / "frontend"
	if not frontend_root.exists():
		raise FileNotFoundError(f"Open WebUI frontend dir not found at {frontend_root}")
	return frontend_root


def _find_open_webui_package_dir() -> Path:
	import open_webui  # noqa: F401

	return Path(open_webui.__file__).resolve().parent


def _find_js_chunks_for_source(frontend_root: Path, source_suffix: str) -> list[Path]:
	chunks_dir = frontend_root / "_app" / "immutable" / "chunks"
	if not chunks_dir.exists():
		raise FileNotFoundError(f"Open WebUI chunks dir not found at {chunks_dir}")

	needle = source_suffix.encode("utf-8")
	js_paths: list[Path] = []
	for map_path in chunks_dir.glob("*.js.map"):
		try:
			if not _contains_bytes(map_path, needle):
				continue
		except OSError:
			continue

		try:
			obj = json.loads(map_path.read_text(encoding="utf-8"))
		except Exception:
			continue

		sources = obj.get("sources") or []
		if not isinstance(sources, list):
			continue

		if not any(isinstance(s, str) and s.endswith(source_suffix) for s in sources):
			continue

		file_rel = obj.get("file")
		if not isinstance(file_rel, str) or not file_rel:
			continue

		js_path = frontend_root / file_rel
		if js_path.exists() and js_path.suffix == ".js":
			js_paths.append(js_path)

	return sorted(set(js_paths))


def _patch_call_overlay_background_audio(js_path: Path, *, dry_run: bool, delay_ms: int) -> tuple[bool, str]:
	"""
	Replace `window.requestAnimationFrame(<cb>)` with `setTimeout(<cb>,delay_ms)` for
	the CallOverlay audio loop. This keeps the loop running (often throttled, but not
	fully paused) when the tab isn't focused/visible.
	"""

	original = js_path.read_text(encoding="utf-8")

	# Fast no-op check.
	if "requestAnimationFrame" not in original:
		return False, "already"

	# Primary (safe) replacement: callback is a simple identifier.
	pattern = re.compile(r"(?:window\.)?requestAnimationFrame\(([A-Za-z_$][0-9A-Za-z_$]*)\)")
	patched, count = pattern.subn(lambda m: f"setTimeout({m.group(1)},{delay_ms})", original)

	if count == 0:
		return False, "unpatched (pattern not found; upstream changed?)"

	if dry_run:
		return True, f"would patch ({count} replacements)"

	js_path.write_text(patched, encoding="utf-8")
	return True, f"patched ({count} replacements)"


def _patch_chat_controls_pane_resize_guard(js_path: Path, *, dry_run: bool) -> tuple[bool, str]:
	"""
	Prevent a ResizeObserver race in ChatControls where the pane can become null
	(e.g. when switching to mobile / hiding the panel) but the observer still fires.
	"""

	original = js_path.read_text(encoding="utf-8")

	# Find the local accessor used for the `pane` prop in this chunk.
	# Example: `...,B=E(n,"pane",12),...`
	m = re.search(r'([A-Za-z_$][0-9A-Za-z_$]*)=E\(n,"pane",12\)', original)
	if not m:
		return False, "unpatched (pane binding not found; upstream changed?)"

	pane = m.group(1)

	# Idempotency.
	if f"{pane}()?.resize" in original or f"{pane}()?.isExpanded" in original:
		return False, "already"

	count_resize = original.count(f"{pane}().resize")
	count_is_expanded = original.count(f"{pane}().isExpanded")
	count_get_size = original.count(f"{pane}().getSize")

	patched = original
	patched = patched.replace(f"{pane}().resize", f"{pane}()?.resize")
	patched = patched.replace(f"{pane}().isExpanded", f"{pane}()?.isExpanded")
	patched = patched.replace(f"{pane}().getSize", f"{pane}()?.getSize")

	if patched == original:
		return False, "unpatched (no changes applied)"

	if dry_run:
		return (
			True,
			f"would patch (pane={pane}, resize={count_resize}, isExpanded={count_is_expanded}, getSize={count_get_size})",
		)

	js_path.write_text(patched, encoding="utf-8")
	return (
		True,
		f"patched (pane={pane}, resize={count_resize}, isExpanded={count_is_expanded}, getSize={count_get_size})",
	)


def _patch_router_audio_cartesia(audio_py_path: Path, *, dry_run: bool) -> tuple[bool, str]:
	"""
	Restore Cartesia support that upstream Open WebUI doesn't ship by default:
	- list Cartesia voices (paginated) for the TTS voice dropdown
	- send Cartesia-Version header for /voices (required)
	- route TTS requests to Cartesia /tts/bytes (Open WebUI expects /audio/speech)
	- normalize Ink Whisper model + locale tags for STT
	"""

	original = audio_py_path.read_text(encoding="utf-8")

	# Idempotency.
	if (
		"def get_cartesia_voices(" in original
		and "Cartesia-Version" in original
		and "/tts/bytes" in original
	):
		return False, "already"

	patched = original

	# Insert helper functions right after the /models route (stable anchor).
	insert_anchor = (
		'@router.get("/models")\n'
		"async def get_models(request: Request, user=Depends(get_verified_user)):\n"
		'    return {"models": get_available_models(request)}\n'
	)
	helpers = (
		"\n"
		"\n"
		"def get_cartesia_version_from_tts_openai_params(request) -> Optional[str]:\n"
		"    params = request.app.state.config.TTS_OPENAI_PARAMS\n"
		"    if isinstance(params, dict):\n"
		'        version = params.get("cartesia_version")\n'
		"        if isinstance(version, str) and version.strip():\n"
		"            return version.strip()\n"
		"    return None\n"
		"\n"
		"\n"
		"@lru_cache\n"
		"def get_cartesia_voices(\n"
		"    api_key: str, base_url: str, cartesia_version: Optional[str]\n"
		") -> dict:\n"
		"    voices: dict[str, str] = {}\n"
		"\n"
		'    headers = {"Authorization": f"Bearer {api_key}"}\n'
		"    if cartesia_version:\n"
		'        headers["Cartesia-Version"] = cartesia_version\n'
		"\n"
		"    starting_after = None\n"
		"    while True:\n"
		'        params = {"limit": 200}\n'
		"        if starting_after:\n"
		'            params["starting_after"] = starting_after\n'
		"\n"
		"        response = requests.get(\n"
		'            f"{base_url.rstrip(\'/\')}/voices",\n'
		"            headers=headers,\n"
		"            params=params,\n"
		"            timeout=10,\n"
		"        )\n"
		"        response.raise_for_status()\n"
		"        data = response.json() or {}\n"
		"\n"
		'        for voice in data.get("data", []) or []:\n'
		'            if isinstance(voice, dict) and voice.get("id") and voice.get("name"):\n'
		'                voices[str(voice["id"])] = str(voice["name"])\n'
		"\n"
		'        next_page = data.get("next_page")\n'
		'        has_more = data.get("has_more")\n'
		"        if not has_more or not next_page:\n"
		"            break\n"
		"\n"
		"        starting_after = str(next_page)\n"
		"\n"
		"    return voices\n"
	)
	if insert_anchor in patched:
		if "def get_cartesia_voices(" not in patched:
			patched = patched.replace(insert_anchor, insert_anchor + helpers)
	else:
		return False, "unpatched (anchor not found; upstream changed?)"

	# Patch TTS voice listing to use Cartesia /voices (requires Cartesia-Version header).
	openai_voices_block = re.compile(
		r"(def get_available_voices\(request\) -> dict:[\s\S]*?\n)"
		r'(\s*if request\.app\.state\.config\.TTS_ENGINE == "openai":\n)'
		r"(?P<body>[\s\S]*?)"
		r'(\n\s*elif request\.app\.state\.config\.TTS_ENGINE == "elevenlabs":\n)',
		re.M,
	)
	new_openai_voices_body = (
		"        base_url = request.app.state.config.TTS_OPENAI_API_BASE_URL or \"\"\n"
		f"        if \"{CARTESIA_HOST_SNIPPET}\" in base_url:\n"
		"            try:\n"
		"                available_voices = get_cartesia_voices(\n"
		"                    api_key=request.app.state.config.TTS_OPENAI_API_KEY,\n"
		"                    base_url=base_url,\n"
		"                    cartesia_version=get_cartesia_version_from_tts_openai_params(request),\n"
		"                )\n"
		"            except Exception as e:\n"
		"                log.error(f\"Error fetching voices from Cartesia: {str(e)}\")\n"
		"                available_voices = {\n"
		"                    \"alloy\": \"alloy\",\n"
		"                    \"echo\": \"echo\",\n"
		"                    \"fable\": \"fable\",\n"
		"                    \"onyx\": \"onyx\",\n"
		"                    \"nova\": \"nova\",\n"
		"                    \"shimmer\": \"shimmer\",\n"
		"                }\n"
		"        # Use custom endpoint if not using the official OpenAI API URL\n"
		"        elif not base_url.startswith(\"https://api.openai.com\"):\n"
		"            try:\n"
		"                response = requests.get(f\"{base_url}/audio/voices\")\n"
		"                response.raise_for_status()\n"
		"                data = response.json()\n"
		"                voices_list = data.get(\"voices\", [])\n"
		"                available_voices = {voice[\"id\"]: voice[\"name\"] for voice in voices_list}\n"
		"            except Exception as e:\n"
		"                log.error(f\"Error fetching voices from custom endpoint: {str(e)}\")\n"
		"                available_voices = {\n"
		"                    \"alloy\": \"alloy\",\n"
		"                    \"echo\": \"echo\",\n"
		"                    \"fable\": \"fable\",\n"
		"                    \"onyx\": \"onyx\",\n"
		"                    \"nova\": \"nova\",\n"
		"                    \"shimmer\": \"shimmer\",\n"
		"                }\n"
		"        else:\n"
		"            available_voices = {\n"
		"                \"alloy\": \"alloy\",\n"
		"                \"echo\": \"echo\",\n"
		"                \"fable\": \"fable\",\n"
		"                \"onyx\": \"onyx\",\n"
		"                \"nova\": \"nova\",\n"
		"                \"shimmer\": \"shimmer\",\n"
		"            }\n"
	)
	m = openai_voices_block.search(patched)
	if not m:
		return False, "unpatched (voice listing block not found; upstream changed?)"
	patched = openai_voices_block.sub(rf"\1\2{new_openai_voices_body}\n\4", patched, count=1)

	# Patch TTS request to include Cartesia-Version header when using Cartesia base URL.
	tts_headers_marker = 'headers = {\n                    "Content-Type": "application/json",\n                    "Authorization": f"Bearer {request.app.state.config.TTS_OPENAI_API_KEY}",\n                }\n'
	if tts_headers_marker in patched and "headers[\"Cartesia-Version\"]" not in patched:
		tts_header_inject = (
			tts_headers_marker
			+ f'                if "{CARTESIA_HOST_SNIPPET}" in request.app.state.config.TTS_OPENAI_API_BASE_URL:\n'
			+ "                    cartesia_version = None\n"
			+ "                    if isinstance(request.app.state.config.TTS_OPENAI_PARAMS, dict):\n"
			+ "                        cartesia_version = request.app.state.config.TTS_OPENAI_PARAMS.get(\n"
			+ '                            "cartesia_version"\n'
			+ "                        )\n"
			+ "                    if cartesia_version:\n"
			+ '                        headers["Cartesia-Version"] = str(cartesia_version)\n'
		)
		patched = patched.replace(tts_headers_marker, tts_header_inject)

	# Patch TTS request URL + payload for Cartesia.
	tts_post_block = (
		"                r = await session.post(\n"
		'                    url=f"{request.app.state.config.TTS_OPENAI_API_BASE_URL}/audio/speech",\n'
		"                    json=payload,\n"
		"                    headers=headers,\n"
		"                    ssl=AIOHTTP_CLIENT_SESSION_SSL,\n"
		"                )\n"
	)
	if tts_post_block in patched and "/tts/bytes" not in patched:
		tts_post_repl = (
			"                tts_base_url = request.app.state.config.TTS_OPENAI_API_BASE_URL.rstrip(\"/\")\n"
			"                tts_url = f\"{tts_base_url}/audio/speech\"\n"
			"\n"
			f"                if \"{CARTESIA_HOST_SNIPPET}\" in tts_base_url:\n"
			"                    tts_url = f\"{tts_base_url}/tts/bytes\"\n"
			"\n"
			"                    voice_id = request.app.state.config.TTS_VOICE\n"
			"                    if isinstance(payload.get(\"voice\"), dict) and payload[\"voice\"].get(\"id\"):\n"
			"                        voice_id = str(payload[\"voice\"][\"id\"])\n"
			"                    elif isinstance(payload.get(\"voice\"), str) and payload.get(\"voice\"):\n"
			"                        voice_id = str(payload[\"voice\"])\n"
			"\n"
			"                    transcript = (\n"
			"                        payload.get(\"transcript\")\n"
			"                        or payload.get(\"input\")\n"
			"                        or payload.get(\"text\")\n"
			"                        or \"\"\n"
			"                    )\n"
			"\n"
			"                    output_format = None\n"
			"                    if isinstance(payload.get(\"output_format\"), dict):\n"
			"                        output_format = payload.get(\"output_format\")\n"
			"                    elif isinstance(request.app.state.config.TTS_OPENAI_PARAMS, dict):\n"
			"                        output_format = request.app.state.config.TTS_OPENAI_PARAMS.get(\n"
			"                            \"output_format\"\n"
			"                        )\n"
			"\n"
			"                    payload = {\n"
			"                        \"model_id\": request.app.state.config.TTS_MODEL,\n"
			"                        \"transcript\": transcript,\n"
			"                        \"voice\": {\"mode\": \"id\", \"id\": voice_id},\n"
			"                    }\n"
			"                    if output_format:\n"
			"                        payload[\"output_format\"] = output_format\n"
			"\n"
			"                r = await session.post(\n"
			"                    url=tts_url,\n"
			"                    json=payload,\n"
			"                    headers=headers,\n"
			"                    ssl=AIOHTTP_CLIENT_SESSION_SSL,\n"
			"                )\n"
		)
		patched = patched.replace(tts_post_block, tts_post_repl)

	# Patch STT request: normalize model/lang + include Cartesia-Version header.
	stt_payload_anchor = (
		"                payload = {\n"
		'                    "model": request.app.state.config.STT_MODEL,\n'
		"                }\n"
		"\n"
		"                if language:\n"
		"                    payload[\"language\"] = language\n"
		"\n"
		"                headers = {\n"
		"                    \"Authorization\": f\"Bearer {request.app.state.config.STT_OPENAI_API_KEY}\"\n"
		"                }\n"
	)
	if stt_payload_anchor in patched:
		stt_payload_repl = (
			"                model = request.app.state.config.STT_MODEL\n"
			+ f"                if (\"{CARTESIA_HOST_SNIPPET}\" in request.app.state.config.STT_OPENAI_API_BASE_URL and model == \"ink/ink-whisper\"):\n"
			+ "                    model = \"ink-whisper\"\n"
			+ "\n"
			+ "                payload = {\"model\": model}\n"
			+ "\n"
			+ "                if language:\n"
			+ f"                    if (\"{CARTESIA_HOST_SNIPPET}\" in request.app.state.config.STT_OPENAI_API_BASE_URL and isinstance(language, str) and \"-\" in language):\n"
			+ "                        language = language.split(\"-\", 1)[0]\n"
			+ "                    payload[\"language\"] = language\n"
			+ "\n"
			+ "                headers = {\n"
			+ "                    \"Authorization\": f\"Bearer {request.app.state.config.STT_OPENAI_API_KEY}\"\n"
			+ "                }\n"
			+ f"                if \"{CARTESIA_HOST_SNIPPET}\" in request.app.state.config.STT_OPENAI_API_BASE_URL:\n"
			+ "                    cartesia_version = None\n"
			+ "                    if isinstance(request.app.state.config.TTS_OPENAI_PARAMS, dict):\n"
			+ "                        cartesia_version = request.app.state.config.TTS_OPENAI_PARAMS.get(\n"
			+ '                            "cartesia_version"\n'
			+ "                        )\n"
			+ "                    if cartesia_version:\n"
			+ '                        headers["Cartesia-Version"] = str(cartesia_version)\n'
		)
		patched = patched.replace(stt_payload_anchor, stt_payload_repl)

	changed = patched != original
	if not changed:
		return False, "unpatched (no changes applied)"

	if dry_run:
		return True, "would patch"

	audio_py_path.write_text(patched, encoding="utf-8")
	return True, "patched"


def _patch_router_openai_provider_timing(openai_py_path: Path, *, dry_run: bool) -> tuple[bool, str]:
	"""
	Add high-signal logging around OpenAI-compatible /chat/completions calls, focusing on Cerebras
	latency/timeouts. This helps diagnose stalls/TTFT/timeouts without enabling full audit logging.
	"""

	original = openai_py_path.read_text(encoding="utf-8")

	# Idempotency.
	if PROVIDER_TIMING_MARKER in original:
		return False, "already"

	patched = original

	# Ensure we can use time.monotonic().
	if "import time\n" not in patched:
		patched, count = re.subn(r"^import json\n", "import json\nimport time\n", patched, count=1, flags=re.M)
		if count == 0:
			return False, "unpatched (import json anchor not found; upstream changed?)"

	# 1) Add local vars right before payload is JSON-encoded (unique anchor in this file).
	dumps_anchor = "    payload = json.dumps(payload)\n"
	if dumps_anchor not in patched:
		return False, "unpatched (payload json.dumps anchor not found; upstream changed?)"

	dumps_repl = (
		f"    # {PROVIDER_TIMING_MARKER}\n"
		"    _owui_provider_selected_model = model_id\n"
		"    _owui_provider_sent_model = payload.get(\"model\") if isinstance(payload, dict) else None\n"
		"    _owui_provider_base_url = url\n"
		"    _owui_provider_request_url = request_url\n"
		"    _owui_provider_is_cerebras = (\n"
		"        isinstance(_owui_provider_base_url, str)\n"
		"        and \"cerebras\" in _owui_provider_base_url.lower()\n"
		"    ) or (\n"
		"        isinstance(_owui_provider_selected_model, str)\n"
		"        and _owui_provider_selected_model.lower().startswith(\"cerebras.\")\n"
		"    )\n"
		"    _owui_provider_t0 = None\n"
		"\n"
		"    payload = json.dumps(payload)\n"
	)
	patched = patched.replace(dumps_anchor, dumps_repl, 1)

	# 2) Start timer + log at the beginning of the provider request.
	try_start_anchor = "    try:\n        session = aiohttp.ClientSession(\n"
	if try_start_anchor not in patched:
		return False, "unpatched (try/session anchor not found; upstream changed?)"

	try_start_repl = (
		"    try:\n"
		"        _owui_provider_t0 = time.monotonic()\n"
		"        if _owui_provider_is_cerebras:\n"
		"            log.info(\n"
		"                f\"[provider] start selected={_owui_provider_selected_model} sent={_owui_provider_sent_model} url={_owui_provider_request_url}\"\n"
		"            )\n"
		"        session = aiohttp.ClientSession(\n"
	)
	patched = patched.replace(try_start_anchor, try_start_repl, 1)

	# 3) Log response headers (status + content-type + time-to-headers).
	request_end_anchor = "        )\n\n        # Check if response is SSE\n"
	if request_end_anchor not in patched:
		return False, "unpatched (request/stream anchor not found; upstream changed?)"

	request_end_repl = (
		"        )\n"
		"\n"
		"        _owui_provider_t_headers = time.monotonic()\n"
		"        _owui_provider_ct = r.headers.get(\"Content-Type\", \"\")\n"
		"        if _owui_provider_is_cerebras:\n"
		"            log.info(\n"
		"                f\"[provider] headers selected={_owui_provider_selected_model} sent={_owui_provider_sent_model} status={r.status} ct={_owui_provider_ct} dt={_owui_provider_t_headers - _owui_provider_t0:.3f}s url={_owui_provider_request_url}\"\n"
		"            )\n"
		"\n"
		"        # Check if response is SSE\n"
	)
	patched = patched.replace(request_end_anchor, request_end_repl, 1)

	# 4) Wrap streaming to log TTFT + stream errors + total time, and ensure cleanup happens.
	streaming_block = (
		'        if "text/event-stream" in r.headers.get("Content-Type", ""):\n'
		"            streaming = True\n"
		"            return StreamingResponse(\n"
		"                stream_chunks_handler(r.content),\n"
		"                status_code=r.status,\n"
		"                headers=dict(r.headers),\n"
		"                background=BackgroundTask(\n"
		"                    cleanup_response, response=r, session=session\n"
		"                ),\n"
		"            )\n"
	)
	if streaming_block not in patched:
		return False, "unpatched (streaming return block not found; upstream changed?)"

	streaming_repl = (
		'        if "text/event-stream" in r.headers.get("Content-Type", ""):\n'
		"            streaming = True\n"
		"\n"
		"            async def _owui_stream():\n"
		"                _owui_first_chunk = True\n"
		"                try:\n"
		"                    async for chunk in stream_chunks_handler(r.content):\n"
		"                        if _owui_first_chunk:\n"
		"                            _owui_first_chunk = False\n"
		"                            if _owui_provider_is_cerebras:\n"
		"                                _owui_ttft = time.monotonic() - _owui_provider_t0\n"
		"                                log.info(\n"
		"                                    f\"[provider] first_chunk selected={_owui_provider_selected_model} sent={_owui_provider_sent_model} ttft={_owui_ttft:.3f}s url={_owui_provider_request_url}\"\n"
		"                                )\n"
		"                        yield chunk\n"
		"                except Exception as e:\n"
		"                    if _owui_provider_is_cerebras:\n"
		"                        _owui_elapsed = time.monotonic() - _owui_provider_t0\n"
		"                        log.exception(\n"
		"                            f\"[provider] stream_error selected={_owui_provider_selected_model} sent={_owui_provider_sent_model} elapsed={_owui_elapsed:.3f}s url={_owui_provider_request_url}: {e}\"\n"
		"                        )\n"
		"                    raise\n"
		"                finally:\n"
		"                    if _owui_provider_is_cerebras:\n"
		"                        _owui_elapsed = time.monotonic() - _owui_provider_t0\n"
		"                        log.info(\n"
		"                            f\"[provider] done selected={_owui_provider_selected_model} sent={_owui_provider_sent_model} status={r.status} elapsed={_owui_elapsed:.3f}s url={_owui_provider_request_url}\"\n"
		"                        )\n"
		"                    await cleanup_response(r, session)\n"
		"\n"
		"            return StreamingResponse(\n"
		"                _owui_stream(),\n"
		"                status_code=r.status,\n"
		"                headers=dict(r.headers),\n"
		"            )\n"
	)
	patched = patched.replace(streaming_block, streaming_repl, 1)

	# 5) Log non-streaming completion time (some providers don't stream).
	non_streaming_anchor = (
		"            try:\n"
		"                response = await r.json()\n"
		"            except Exception as e:\n"
		"                log.error(e)\n"
		"                response = await r.text()\n"
		"\n"
		"            if r.status >= 400:\n"
	)
	if non_streaming_anchor not in patched:
		return False, "unpatched (non-streaming parse block not found; upstream changed?)"

	non_streaming_repl = (
		"            try:\n"
		"                response = await r.json()\n"
		"            except Exception as e:\n"
		"                log.error(e)\n"
		"                response = await r.text()\n"
		"\n"
		"            if _owui_provider_is_cerebras:\n"
		"                _owui_elapsed = time.monotonic() - _owui_provider_t0\n"
		"                log.info(\n"
		"                    f\"[provider] done selected={_owui_provider_selected_model} sent={_owui_provider_sent_model} status={r.status} ct={_owui_provider_ct} elapsed={_owui_elapsed:.3f}s url={_owui_provider_request_url}\"\n"
		"                )\n"
		"\n"
		"            if r.status >= 400:\n"
	)
	patched = patched.replace(non_streaming_anchor, non_streaming_repl, 1)

	# 6) Add context to exceptions without enabling audit logging.
	exception_anchor = "    except Exception as e:\n        log.exception(e)\n"
	if exception_anchor not in patched:
		return False, "unpatched (exception handler anchor not found; upstream changed?)"

	exception_repl = (
		"    except Exception as e:\n"
		"        if _owui_provider_is_cerebras:\n"
		"            _owui_now = time.monotonic()\n"
		"            _owui_elapsed = _owui_now - (_owui_provider_t0 or _owui_now)\n"
		"            log.exception(\n"
		"                f\"[provider] error selected={_owui_provider_selected_model} sent={_owui_provider_sent_model} elapsed={_owui_elapsed:.3f}s url={_owui_provider_request_url}: {e}\"\n"
		"            )\n"
		"        else:\n"
		"            log.exception(e)\n"
	)
	patched = patched.replace(exception_anchor, exception_repl, 1)

	if patched == original:
		return False, "unpatched (no changes applied)"

	if dry_run:
		return True, "would patch"

	openai_py_path.write_text(patched, encoding="utf-8")
	return True, "patched"


def main(argv: list[str]) -> int:
	parser = argparse.ArgumentParser(
		description="Apply local Open WebUI patches inside the Poetry venv (site-packages)."
	)
	parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing.")
	parser.add_argument(
		"--call-overlay-delay-ms",
		type=int,
		default=50,
		help="Timer delay used to replace requestAnimationFrame in CallOverlay (default: 50).",
	)
	args = parser.parse_args(argv)

	package_dir = _find_open_webui_package_dir()
	frontend_root = package_dir / "frontend"
	if not frontend_root.exists():
		raise FileNotFoundError(f"Open WebUI frontend dir not found at {frontend_root}")
	call_overlay_js_chunks = _find_js_chunks_for_source(frontend_root, CALL_OVERLAY_SOURCE_SUFFIX)
	chat_controls_js_chunks = _find_js_chunks_for_source(frontend_root, CHAT_CONTROLS_SOURCE_SUFFIX)

	if not call_overlay_js_chunks:
		print(
			f"[warn] No chunk found for {CALL_OVERLAY_SOURCE_SUFFIX}. "
			"Open WebUI may have changed its build output.",
			file=sys.stderr,
		)
	if not chat_controls_js_chunks:
		print(
			f"[warn] No chunk found for {CHAT_CONTROLS_SOURCE_SUFFIX}. "
			"Open WebUI may have changed its build output.",
			file=sys.stderr,
		)

	audio_py_path = package_dir / "routers" / "audio.py"
	if not audio_py_path.exists():
		print(f"[warn] Open WebUI audio router not found at {audio_py_path}", file=sys.stderr)
	else:
		applied, msg = _patch_router_audio_cartesia(audio_py_path, dry_run=args.dry_run)
		print(f"{'DRY-RUN ' if args.dry_run else ''}[audio-cartesia] {audio_py_path}: {msg}")

	openai_py_path = package_dir / "routers" / "openai.py"
	if not openai_py_path.exists():
		print(f"[warn] Open WebUI openai router not found at {openai_py_path}", file=sys.stderr)
	else:
		applied, msg = _patch_router_openai_provider_timing(openai_py_path, dry_run=args.dry_run)
		print(f"{'DRY-RUN ' if args.dry_run else ''}[openai-provider-timing] {openai_py_path}: {msg}")

	any_applied = False
	for js_path in call_overlay_js_chunks:
		applied, msg = _patch_call_overlay_background_audio(
			js_path, dry_run=args.dry_run, delay_ms=args.call_overlay_delay_ms
		)
		any_applied = any_applied or applied
		print(f"{'DRY-RUN ' if args.dry_run else ''}[call-overlay] {js_path}: {msg}")

	for js_path in chat_controls_js_chunks:
		applied, msg = _patch_chat_controls_pane_resize_guard(js_path, dry_run=args.dry_run)
		any_applied = any_applied or applied
		print(f"{'DRY-RUN ' if args.dry_run else ''}[chat-controls] {js_path}: {msg}")

	return 0


if __name__ == "__main__":
	raise SystemExit(main(sys.argv[1:]))
