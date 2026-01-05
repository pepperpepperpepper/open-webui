import asyncio
import json
import os
import time
from typing import Any

from livekit import agents
from livekit import rtc
from livekit.agents.voice import events as voice_events
from livekit.agents import Agent, AgentServer, AgentSession
from livekit.agents.voice import room_io
from livekit.agents.log import logger
from livekit.plugins import cartesia, openai


AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "owui-voice")

STT_MODEL = os.getenv("LIVEKIT_STT_MODEL", "ink-whisper")
STT_LANGUAGE = os.getenv("LIVEKIT_STT_LANGUAGE", "en")

LLM_MODEL = os.getenv("LIVEKIT_LLM_MODEL", "zai-glm-4.6")

TTS_MODEL = os.getenv("LIVEKIT_TTS_MODEL", "sonic-2")
TTS_VOICE = os.getenv("LIVEKIT_TTS_VOICE", "").strip()

TURN_DETECTION = os.getenv("LIVEKIT_TURN_DETECTION", "ml").strip().lower()
STARTUP_MESSAGE = os.getenv("LIVEKIT_STARTUP_MESSAGE", "Connected. You can start talking.").strip()

DEBUG_DATA_TOPIC = os.getenv("LIVEKIT_DEBUG_DATA_TOPIC", "owui.voice").strip()
DEBUG_DATA_ENABLED = os.getenv("LIVEKIT_DEBUG_DATA_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
CONTROL_DATA_TOPIC = os.getenv("LIVEKIT_CONTROL_DATA_TOPIC", "owui.voice.control").strip()
MAX_CONTEXT_CHARS = int(os.getenv("LIVEKIT_MAX_CONTEXT_CHARS", "50000"))


def _turn_detection(mode: str) -> Any:
    if mode in ("multilingual", "ml"):
        try:
            from livekit.plugins.turn_detector.multilingual import MultilingualModel

            return MultilingualModel()
        except Exception:
            return "stt"

    return "stt"


def _parse_room_voice_settings(room_metadata: str | None) -> dict[str, object]:
    raw = (room_metadata or "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except Exception:
        return {}

    if not isinstance(parsed, dict):
        return {}

    settings = parsed.get("owui_voice")
    if isinstance(settings, dict):
        return settings

    return {}


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "y", "on"):
            return True
        if normalized in ("0", "false", "no", "n", "off"):
            return False
    return None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except Exception:
            return None
    return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, (float,)) and not isinstance(value, bool):
        if value.is_integer():
            return int(value)
        return None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except Exception:
            return None
    return None


class VoiceAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are the user's real-time voice assistant inside Open WebUI. "
                "Keep replies concise and speak naturally. "
                "Ask a clarifying question when needed."
            )
        )


server = AgentServer()


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: agents.JobContext):
    # Ensure room metadata (turn detection + timing overrides) is available.
    await ctx.connect()

    voice_settings = _parse_room_voice_settings(getattr(ctx.room, "metadata", None))

    turn_detection_mode = (
        str(voice_settings.get("turn_detection", "")).strip().lower() or TURN_DETECTION
    )

    allow_interruptions = _coerce_bool(voice_settings.get("allow_interruptions"))
    min_endpointing_delay = _coerce_float(voice_settings.get("min_endpointing_delay"))
    max_endpointing_delay = _coerce_float(voice_settings.get("max_endpointing_delay"))
    min_interruption_duration = _coerce_float(
        voice_settings.get("min_interruption_duration")
    )
    min_interruption_words = _coerce_int(voice_settings.get("min_interruption_words"))
    tts_voice_override = str(voice_settings.get("tts_voice", "")).strip()

    session_kwargs: dict[str, object] = {
        "turn_detection": _turn_detection(turn_detection_mode),
    }
    if allow_interruptions is not None:
        session_kwargs["allow_interruptions"] = allow_interruptions
    if min_endpointing_delay is not None and 0.0 <= min_endpointing_delay <= 10.0:
        session_kwargs["min_endpointing_delay"] = min_endpointing_delay
    if max_endpointing_delay is not None and 0.0 <= max_endpointing_delay <= 10.0:
        session_kwargs["max_endpointing_delay"] = max_endpointing_delay
    if (
        min_interruption_duration is not None
        and 0.0 <= min_interruption_duration <= 10.0
    ):
        session_kwargs["min_interruption_duration"] = min_interruption_duration
    if min_interruption_words is not None and 0 <= min_interruption_words <= 50:
        session_kwargs["min_interruption_words"] = min_interruption_words

    # Safety: if both endpointing delays are present, enforce min <= max.
    min_ed = session_kwargs.get("min_endpointing_delay")
    max_ed = session_kwargs.get("max_endpointing_delay")
    if isinstance(min_ed, (int, float)) and isinstance(max_ed, (int, float)) and min_ed > max_ed:
        session_kwargs["min_endpointing_delay"] = max_ed
        session_kwargs["max_endpointing_delay"] = min_ed

    session_kwargs_log = {k: v for k, v in session_kwargs.items() if k != "turn_detection"}

    resolved_tts_voice = tts_voice_override or TTS_VOICE

    logger.info(
        "starting session",
        extra={
            "agent_name": AGENT_NAME,
            "stt_model": STT_MODEL,
            "stt_language": STT_LANGUAGE,
            "llm_model": LLM_MODEL,
            "tts_model": TTS_MODEL,
            "tts_voice": resolved_tts_voice or None,
            "turn_detection": turn_detection_mode,
            "session_kwargs": session_kwargs_log,
        },
    )

    tts_kwargs = {"model": TTS_MODEL}
    if resolved_tts_voice:
        tts_kwargs["voice"] = resolved_tts_voice

    session = AgentSession(
        # STT: Cartesia Ink Whisper
        stt=cartesia.STT(model=STT_MODEL, language=STT_LANGUAGE),
        # LLM: Cerebras Cloud (OpenAI-compatible) — set LLM_MODEL to e.g. "zai-glm-4.6"
        llm=openai.LLM.with_cerebras(model=LLM_MODEL),
        # TTS: Cartesia Sonic
        tts=cartesia.TTS(**tts_kwargs),
        **session_kwargs,
    )
    voice_agent = VoiceAgent()
    context_lock = asyncio.Lock()

    async def publish_debug_event(payload: dict[str, object], *, reliable: bool = True) -> None:
        if not DEBUG_DATA_ENABLED or not DEBUG_DATA_TOPIC:
            return
        try:
            payload_with_meta = {
                **payload,
                "ts": time.time(),
                "agent_name": AGENT_NAME,
                "llm_model": LLM_MODEL,
                "stt_model": STT_MODEL,
                "tts_model": TTS_MODEL,
            }
            await ctx.room.local_participant.publish_data(
                json.dumps(payload_with_meta, separators=(",", ":"), ensure_ascii=False),
                reliable=reliable,
                topic=DEBUG_DATA_TOPIC,
            )
        except Exception:
            logger.exception("publish_data failed", extra={"topic": DEBUG_DATA_TOPIC})

    def schedule_publish(payload: dict[str, object]) -> None:
        asyncio.create_task(publish_debug_event(payload), name="publish_debug_event")

    async def apply_context(text: str, *, mode: str, request_id: str | None = None) -> None:
        async with context_lock:
            text = text.strip()

            def is_context_item(item: object) -> bool:
                try:
                    if getattr(item, "type", None) != "message":
                        return False
                    extra = getattr(item, "extra", None)
                    return isinstance(extra, dict) and extra.get("owui_livekit_context") is True
                except Exception:
                    return False

            def find_last_context_message(chat_ctx: object) -> object | None:
                try:
                    items = getattr(chat_ctx, "items", None) or []
                except Exception:
                    items = []
                try:
                    for item in reversed(list(items)):
                        if is_context_item(item):
                            return item
                except Exception:
                    return None
                return None

            def context_total_chars(chat_ctx: object) -> int:
                total = 0
                try:
                    items = getattr(chat_ctx, "items", None) or []
                except Exception:
                    items = []
                for item in items:
                    if not is_context_item(item):
                        continue
                    try:
                        content = getattr(item, "content", None)
                    except Exception:
                        content = None
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, str):
                                total += len(part)
                    elif isinstance(content, str):
                        total += len(content)
                return total

            if mode not in ("replace", "append"):
                mode = "replace"

            # Work on a mutable copy, then update the agent properly.
            try:
                chat_ctx = voice_agent.chat_ctx.copy()
            except Exception:
                logger.exception("failed to copy agent chat_ctx")
                schedule_publish(
                    {
                        "type": "owui.voice.event",
                        "event": "context_error",
                        "data": {"message": "Failed to access chat context", "request_id": request_id},
                    }
                )
                return

            # Remove existing context items first when replacing.
            if mode == "replace":
                try:
                    chat_ctx.items[:] = [i for i in chat_ctx.items if not is_context_item(i)]
                except Exception:
                    logger.exception("failed to clear existing context")

            if not text:
                try:
                    await voice_agent.update_chat_ctx(chat_ctx)
                except Exception:
                    logger.exception("failed to update chat_ctx after context clear")
                schedule_publish(
                    {
                        "type": "owui.voice.event",
                        "event": "context_cleared",
                        "data": {"mode": mode, "request_id": request_id},
                    }
                )
                return

            truncated = False
            if MAX_CONTEXT_CHARS > 0:
                current_chars = context_total_chars(chat_ctx)
                remaining = MAX_CONTEXT_CHARS - current_chars
                if remaining <= 0:
                    schedule_publish(
                        {
                            "type": "owui.voice.event",
                            "event": "context_error",
                            "data": {
                                "message": "Context is full (max chars reached)",
                                "max_chars": MAX_CONTEXT_CHARS,
                                "chars": current_chars,
                                "request_id": request_id,
                            },
                        }
                    )
                    return
                if len(text) > remaining:
                    text = text[:remaining]
                    truncated = True

            appended = False
            msg = None

            if mode == "append":
                existing = find_last_context_message(chat_ctx)
                if existing is not None:
                    try:
                        content = getattr(existing, "content", None)
                        if isinstance(content, list):
                            content.append(text)
                            msg = existing
                            appended = True
                        else:
                            # Broken/legacy content shape; fall back to replacing with a fresh message.
                            msg = None
                    except Exception:
                        logger.exception("failed to append to existing context message")

            if msg is None:
                wrapped = f"Reference context (provided by user):\n\n{text}"
                try:
                    msg = chat_ctx.add_message(
                        role="system",
                        content=wrapped,
                        extra={"owui_livekit_context": True},
                    )
                except Exception:
                    logger.exception("failed to add context message to chat_ctx")
                    schedule_publish(
                        {
                            "type": "owui.voice.event",
                            "event": "context_error",
                            "data": {"message": "Failed to add context", "request_id": request_id},
                        }
                    )
                    return

            try:
                await voice_agent.update_chat_ctx(chat_ctx)
            except Exception:
                logger.exception("failed to update agent chat_ctx after context set")
                schedule_publish(
                    {
                        "type": "owui.voice.event",
                        "event": "context_error",
                        "data": {"message": "Failed to apply context to session", "request_id": request_id},
                    }
                )
                return

            schedule_publish(
                {
                    "type": "owui.voice.event",
                    "event": "context_set",
                    "data": {
                        "mode": mode,
                        "chars": len(text),
                        "id": getattr(msg, "id", None),
                        "appended": appended,
                        "truncated": truncated,
                        "max_chars": MAX_CONTEXT_CHARS,
                        "total_chars": context_total_chars(chat_ctx),
                        "request_id": request_id,
                    },
                }
            )

    async def clear_context(*, request_id: str | None = None) -> None:
        async with context_lock:
            def is_context_item(item: object) -> bool:
                try:
                    if getattr(item, "type", None) != "message":
                        return False
                    extra = getattr(item, "extra", None)
                    return isinstance(extra, dict) and extra.get("owui_livekit_context") is True
                except Exception:
                    return False

            try:
                chat_ctx = voice_agent.chat_ctx.copy()
                chat_ctx.items[:] = [i for i in chat_ctx.items if not is_context_item(i)]
                await voice_agent.update_chat_ctx(chat_ctx)
            except Exception:
                logger.exception("failed to clear context items")

            schedule_publish(
                {
                    "type": "owui.voice.event",
                    "event": "context_cleared",
                    "data": {"request_id": request_id},
                }
            )

    def on_data_received(packet: rtc.DataPacket) -> None:
        if not CONTROL_DATA_TOPIC:
            return

        sender = packet.participant
        sender_identity = str(getattr(sender, "identity", "") or "")

        try:
            raw = (packet.data or b"").decode("utf-8", errors="replace").strip()
        except Exception:
            raw = ""
        if not raw:
            return

        # If this *looks* like a control payload but was published on the wrong topic, log it.
        if packet.topic != CONTROL_DATA_TOPIC:
            if "owui.voice.control" in raw:
                logger.warning(
                    "Control payload received on wrong topic",
                    extra={"topic": packet.topic, "identity": sender_identity},
                )
            return

        if sender_identity and not sender_identity.startswith("owui:"):
            logger.warning(
                "Ignoring control packet from non-owui identity",
                extra={"identity": sender_identity, "topic": packet.topic},
            )
            return

        try:
            payload = json.loads(raw)
        except Exception:
            schedule_publish(
                {
                    "type": "owui.voice.event",
                    "event": "context_error",
                    "data": {"message": "Invalid JSON control payload"},
                }
            )
            return

        if not isinstance(payload, dict) or payload.get("type") != "owui.voice.control":
            return

        op = str(payload.get("op") or "").strip().lower()
        request_id = str(payload.get("request_id") or "").strip() or None
        if op == "context_set":
            text = str(payload.get("text") or "")
            mode = str(payload.get("mode") or "replace").strip().lower()
            logger.info(
                "control_context_set",
                extra={
                    "identity": sender_identity,
                    "topic": packet.topic,
                    "request_id": request_id,
                    "chars": len(text),
                    "mode": mode,
                },
            )
            asyncio.create_task(apply_context(text, mode=mode, request_id=request_id), name="context_set")
        elif op == "context_clear":
            logger.info(
                "control_context_clear",
                extra={"identity": sender_identity, "topic": packet.topic, "request_id": request_id},
            )
            asyncio.create_task(clear_context(request_id=request_id), name="context_clear")
        else:
            schedule_publish(
                {
                    "type": "owui.voice.event",
                    "event": "context_error",
                    "data": {"message": f"Unknown control op: {op or '(empty)'}", "request_id": request_id},
                }
            )

    # Stream high-level agent/session signals to the browser demo via LiveKit data packets.
    def on_agent_state_changed(ev: voice_events.AgentStateChangedEvent) -> None:
        logger.info(
            "agent_state_changed",
            extra={"old_state": ev.old_state, "new_state": ev.new_state},
        )
        schedule_publish(
            {
                "type": "owui.voice.event",
                "event": "agent_state_changed",
                "data": ev.model_dump(),
            }
        )

    def on_user_state_changed(ev: voice_events.UserStateChangedEvent) -> None:
        schedule_publish(
            {
                "type": "owui.voice.event",
                "event": "user_state_changed",
                "data": ev.model_dump(),
            }
        )

    def on_user_input_transcribed(ev: voice_events.UserInputTranscribedEvent) -> None:
        # Emitted for both partial and final transcripts.
        if ev.is_final:
            logger.info(
                "user_input_transcribed_final",
                extra={
                    "language": ev.language,
                    "transcript": (ev.transcript or "")[:500],
                },
            )
        schedule_publish(
            {
                "type": "owui.voice.event",
                "event": "user_input_transcribed",
                "data": ev.model_dump(),
            }
        )

    def on_conversation_item_added(ev: voice_events.ConversationItemAddedEvent) -> None:
        # Only send a compact view to avoid huge payloads.
        item = ev.item
        payload: dict[str, object] = {
            "type": "owui.voice.event",
            "event": "conversation_item_added",
        }
        try:
            payload["data"] = {
                "id": getattr(item, "id", None),
                "role": getattr(item, "role", None),
                "text": getattr(item, "text_content", None),
                "interrupted": getattr(item, "interrupted", None),
                "metrics": getattr(item, "metrics", None),
                "created_at": getattr(item, "created_at", None),
            }
        except Exception:
            payload["data"] = {"repr": repr(item)}

        schedule_publish(payload)

    def on_agent_false_interruption(ev: voice_events.AgentFalseInterruptionEvent) -> None:
        data = ev.model_dump()
        for key in ("message", "extra_instructions"):
            if isinstance(data.get(key), str) and len(data[key]) > 800:
                data[key] = f"{data[key][:800]}…"
        schedule_publish(
            {
                "type": "owui.voice.event",
                "event": "agent_false_interruption",
                "data": data,
            }
        )

    def on_metrics_collected(ev: voice_events.MetricsCollectedEvent) -> None:
        schedule_publish(
            {
                "type": "owui.voice.event",
                "event": "metrics_collected",
                "data": ev.model_dump(),
            }
        )

    def on_speech_created(ev: voice_events.SpeechCreatedEvent) -> None:
        speech = ev.speech_handle
        speech_info: dict[str, object]
        try:
            speech_info = {
                "id": getattr(speech, "id", None),
                "allow_interruptions": getattr(speech, "allow_interruptions", None),
                "scheduled": getattr(speech, "scheduled", None),
                "interrupted": getattr(speech, "interrupted", None),
                "num_steps": getattr(speech, "num_steps", None),
            }
        except Exception:
            speech_info = {"repr": repr(speech)}

        schedule_publish(
            {
                "type": "owui.voice.event",
                "event": "speech_created",
                "data": {
                    "user_initiated": ev.user_initiated,
                    "source": ev.source,
                    "speech": speech_info,
                    "created_at": ev.created_at,
                },
            }
        )

    def on_error(ev: voice_events.ErrorEvent) -> None:
        data = ev.model_dump()

        err = ev.error
        try:
            exc = getattr(err, "error", None)
        except Exception:
            exc = None

        if exc is not None:
            data["error_detail"] = {
                "exception_type": type(exc).__name__,
                "exception": str(exc),
            }

        schedule_publish(
            {
                "type": "owui.voice.event",
                "event": "error",
                "data": data,
            }
        )

    def on_function_tools_executed(ev: voice_events.FunctionToolsExecutedEvent) -> None:
        def clip(value: str, max_len: int = 800) -> str:
            if len(value) <= max_len:
                return value
            return f"{value[:max_len]}…"

        calls = []
        try:
            for fc in ev.function_calls or []:
                calls.append(
                    {
                        "name": getattr(fc, "name", None),
                        "call_id": getattr(fc, "call_id", None),
                        "arguments": clip(str(getattr(fc, "arguments", "") or "")),
                        "created_at": getattr(fc, "created_at", None),
                    }
                )
        except Exception:
            calls = [{"error": "Failed to serialize function_calls"}]

        outputs = []
        try:
            for out in ev.function_call_outputs or []:
                if out is None:
                    outputs.append(None)
                    continue
                outputs.append(
                    {
                        "name": getattr(out, "name", None),
                        "call_id": getattr(out, "call_id", None),
                        "is_error": getattr(out, "is_error", None),
                        "output": clip(str(getattr(out, "output", "") or "")),
                        "created_at": getattr(out, "created_at", None),
                    }
                )
        except Exception:
            outputs = [{"error": "Failed to serialize function_call_outputs"}]

        schedule_publish(
            {
                "type": "owui.voice.event",
                "event": "function_tools_executed",
                "data": {
                    "function_calls": calls,
                    "function_call_outputs": outputs,
                    "created_at": ev.created_at,
                },
            }
        )

    def on_close(ev: voice_events.CloseEvent) -> None:
        logger.warning(
            "session_close",
            extra={"reason": ev.reason, "error": str(ev.error) if ev.error else None},
        )
        schedule_publish(
            {
                "type": "owui.voice.event",
                "event": "close",
                "data": {
                    "reason": str(ev.reason),
                    "error": str(ev.error) if ev.error else None,
                    "created_at": ev.created_at,
                },
            }
        )

    session.on("agent_state_changed", on_agent_state_changed)
    session.on("user_state_changed", on_user_state_changed)
    session.on("user_input_transcribed", on_user_input_transcribed)
    session.on("conversation_item_added", on_conversation_item_added)
    session.on("agent_false_interruption", on_agent_false_interruption)
    session.on("function_tools_executed", on_function_tools_executed)
    session.on("metrics_collected", on_metrics_collected)
    session.on("speech_created", on_speech_created)
    session.on("error", on_error)
    session.on("close", on_close)

    schedule_publish(
        {
            "type": "owui.voice.event",
            "event": "agent_starting",
            "data": {
                "turn_detection": turn_detection_mode,
                "session_kwargs": session_kwargs_log,
                "tts_voice": resolved_tts_voice or None,
                "llm_model": LLM_MODEL,
                "stt_model": STT_MODEL,
                "stt_language": STT_LANGUAGE,
                "tts_model": TTS_MODEL,
            },
        }
    )

    ctx.room.on("data_received", on_data_received)
    await session.start(
        room=ctx.room,
        agent=voice_agent,
        room_input_options=room_io.RoomInputOptions(close_on_disconnect=False),
    )
    if STARTUP_MESSAGE:
        try:
            await session.say(STARTUP_MESSAGE, add_to_chat_ctx=False)
        except Exception:
            logger.exception("startup TTS failed")


if __name__ == "__main__":
    agents.cli.run_app(server)
