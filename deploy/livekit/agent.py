import asyncio
import json
import os
import time
from typing import Any

from livekit import agents
from livekit import rtc
from livekit.agents.voice import events as voice_events
from livekit.agents import Agent, AgentServer, AgentSession, StopResponse, llm
from livekit.agents.tokenize.basic import split_words
from livekit.agents.voice import room_io
from livekit.agents.log import logger
from livekit.plugins import cartesia, openai


AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "owui-voice")
LIVEKIT_HTTP_URL = os.getenv("LIVEKIT_HTTP_URL", "").strip()
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "").strip()
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "").strip()

STT_MODEL = os.getenv("LIVEKIT_STT_MODEL", "ink-whisper")
STT_LANGUAGE = os.getenv("LIVEKIT_STT_LANGUAGE", "en")

LLM_MODEL = os.getenv("LIVEKIT_LLM_MODEL", "zai-glm-4.7")

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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "y", "on"):
        return True
    if lowered in ("0", "false", "no", "n", "off"):
        return False
    return default


def _normalize_livekit_http_url(value: str) -> str:
    raw = (value or "").strip()
    if raw.startswith("ws://"):
        return f"http://{raw.removeprefix('ws://')}"
    if raw.startswith("wss://"):
        return f"https://{raw.removeprefix('wss://')}"
    return raw


NUM_IDLE_PROCESSES = _env_int("LIVEKIT_NUM_IDLE_PROCESSES", 0)
JOB_MEMORY_WARN_MB = _env_float("LIVEKIT_JOB_MEMORY_WARN_MB", 256.0)
JOB_MEMORY_LIMIT_MB = _env_float("LIVEKIT_JOB_MEMORY_LIMIT_MB", 384.0)
SHUTDOWN_PROCESS_TIMEOUT_SEC = _env_float("LIVEKIT_SHUTDOWN_PROCESS_TIMEOUT_SEC", 3.0)
LLM_ERROR_SPOKEN_COOLDOWN_SEC = _env_float("LIVEKIT_LLM_ERROR_SPOKEN_COOLDOWN_SEC", 12.0)
LLM_FAILURE_MESSAGE = os.getenv("LIVEKIT_LLM_FAILURE_MESSAGE", "").strip()
SESSION_IDLE_TIMEOUT_SEC = _env_float("LIVEKIT_SESSION_IDLE_TIMEOUT_SEC", 900.0)
SESSION_IDLE_CHECK_INTERVAL_SEC = _env_float(
    "LIVEKIT_SESSION_IDLE_CHECK_INTERVAL_SEC",
    15.0,
)
SESSION_IDLE_FORCE_DELETE = _env_bool("LIVEKIT_SESSION_IDLE_FORCE_DELETE", True)
STT_FINAL_DEDUP_WINDOW_SEC = _env_float("LIVEKIT_STT_FINAL_DEDUP_WINDOW_SEC", 8.0)
SUPPRESS_FRAGMENT_TURNS = _env_bool("LIVEKIT_SUPPRESS_FRAGMENT_TURNS", True)
FRAGMENT_TURN_MAX_WORDS = max(0, _env_int("LIVEKIT_FRAGMENT_TURN_MAX_WORDS", 1))
FRAGMENT_TURN_MAX_CHARS = max(0, _env_int("LIVEKIT_FRAGMENT_TURN_MAX_CHARS", 24))


def _normalize_fragment_turn_text(value: object) -> str:
    text = " ".join(str(value or "").split()).strip().casefold()
    return text.strip(" \t\r\n.,!?;:-—\"'()[]{}")


def _env_csv_set(name: str, default: str) -> set[str]:
    raw = os.getenv(name, default)
    values = set()
    for part in raw.split(","):
        normalized = _normalize_fragment_turn_text(part)
        if normalized:
            values.add(normalized)
    return values


FRAGMENT_TURN_ALLOWLIST = _env_csv_set(
    "LIVEKIT_FRAGMENT_TURN_ALLOWLIST",
    "yes,no,ok,okay,hello,hi,hey,help,stop,repeat,continue",
)
FRAGMENT_TURN_BLOCKLIST = _env_csv_set(
    "LIVEKIT_FRAGMENT_TURN_BLOCKLIST",
    "what,when,where,why,who,whom,whose,which,how,thank,thanks",
)


def _spoken_model_name(model: str) -> str:
    lowered = (model or "").strip().lower()
    if lowered == "zai-glm-4.7":
        return "GLM 4.7"
    if lowered == "zai-glm-4.6":
        return "GLM 4.6"
    return "the language model"


def _default_llm_failure_message(model: str) -> str:
    if LLM_FAILURE_MESSAGE:
        return LLM_FAILURE_MESSAGE
    return (
        f"I'm having trouble getting a response from {_spoken_model_name(model)} right now. "
        "The model server may be overloaded. Please try again in a moment."
    )


def _normalize_transcript_key(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text.casefold()


def _should_suppress_fragment_turn(text: object) -> tuple[bool, dict[str, object]]:
    normalized = _normalize_fragment_turn_text(text)
    details: dict[str, object] = {
        "normalized_text": normalized,
        "word_count": 0,
        "char_count": len(normalized),
        "reason": None,
    }

    if not SUPPRESS_FRAGMENT_TURNS or not normalized:
        return False, details
    if normalized in FRAGMENT_TURN_ALLOWLIST:
        details["reason"] = "allowlist"
        return False, details

    word_count = len(split_words(normalized, ignore_punctuation=True))
    details["word_count"] = word_count

    if FRAGMENT_TURN_MAX_WORDS > 0 and word_count > FRAGMENT_TURN_MAX_WORDS:
        details["reason"] = "too_many_words"
        return False, details
    if FRAGMENT_TURN_MAX_CHARS > 0 and len(normalized) > FRAGMENT_TURN_MAX_CHARS:
        details["reason"] = "too_many_chars"
        return False, details
    if normalized in FRAGMENT_TURN_BLOCKLIST:
        details["reason"] = "blocklist"
        return True, details

    return False, details


async def _cleanup_room_if_empty(
    room_name: str,
    *,
    trigger_reason: str,
    force: bool = False,
) -> bool:
    room_name = str(room_name or "").strip()
    if not room_name:
        return False

    livekit_http_url = LIVEKIT_HTTP_URL or _normalize_livekit_http_url(
        os.getenv("LIVEKIT_URL", "")
    )
    if not livekit_http_url or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        logger.warning(
            "Skipping empty-room cleanup; missing LiveKit admin configuration",
            extra={"room": room_name, "trigger_reason": trigger_reason},
        )
        return False

    from livekit import api

    lk = api.LiveKitAPI(
        url=livekit_http_url,
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET,
    )

    try:
        existing = await lk.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
        if not existing.rooms:
            return False

        participants = await lk.room.list_participants(
            api.ListParticipantsRequest(room=room_name)
        )
        human_identities = [
            identity
            for p in (participants.participants or [])
            if (identity := str(getattr(p, "identity", "") or "").strip())
            and not identity.startswith("agent-")
        ]
        if human_identities and not force:
            logger.info(
                "Skipping empty-room cleanup; human participants remain",
                extra={
                    "room": room_name,
                    "trigger_reason": trigger_reason,
                    "force": force,
                    "participants": human_identities,
                },
            )
            return False
        if human_identities and force:
            logger.warning(
                "Force-deleting room with remaining human participants",
                extra={
                    "room": room_name,
                    "trigger_reason": trigger_reason,
                    "force": force,
                    "participants": human_identities,
                },
            )

        try:
            dispatches = await lk.agent_dispatch.list_dispatch(room_name=room_name)
            for d in dispatches:
                if getattr(d, "agent_name", None) == AGENT_NAME:
                    await lk.agent_dispatch.delete_dispatch(d.id, room_name)
        except Exception:
            logger.exception(
                "Failed to delete agent dispatch during empty-room cleanup",
                extra={"room": room_name, "trigger_reason": trigger_reason, "force": force},
            )

        await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
        logger.info(
            "Deleted empty room after agent close",
            extra={"room": room_name, "trigger_reason": trigger_reason, "force": force},
        )
        return True
    except Exception:
        logger.exception(
            "Empty-room cleanup failed",
            extra={"room": room_name, "trigger_reason": trigger_reason, "force": force},
        )
        return False
    finally:
        await lk.aclose()


def _turn_detection(mode: str) -> Any:
    if mode in ("multilingual", "ml"):
        if not TURN_DETECTOR_PLUGIN_READY:
            logger.warning(
                "turn_detection_fallback_to_stt",
                extra={
                    "requested_mode": mode,
                    "reason": "turn-detector plugin not registered at worker startup",
                },
            )
            return "stt"
        try:
            from livekit.plugins.turn_detector.multilingual import MultilingualModel

            return MultilingualModel()
        except Exception as exc:
            logger.warning(
                "turn_detection_fallback_to_stt",
                extra={
                    "requested_mode": mode,
                    "reason": f"{type(exc).__name__}: {exc}"[:500],
                },
            )
            return "stt"

    return "stt"


def _has_local_multilingual_turn_detector_assets() -> bool:
    try:
        from livekit.plugins.turn_detector.base import _download_from_hf_hub
        from livekit.plugins.turn_detector.models import (
            HG_MODEL,
            MODEL_REVISIONS,
            ONNX_FILENAME,
        )

        revision = MODEL_REVISIONS["multilingual"]
        _download_from_hf_hub(
            HG_MODEL,
            "languages.json",
            revision=revision,
            local_files_only=True,
        )
        _download_from_hf_hub(
            HG_MODEL,
            ONNX_FILENAME,
            subfolder="onnx",
            revision=revision,
            local_files_only=True,
        )
        return True
    except Exception:
        return False


def _register_turn_detector_plugin_if_available() -> bool:
    remote_eot_url = os.getenv("LIVEKIT_REMOTE_EOT_URL", "").strip()
    if not remote_eot_url and not _has_local_multilingual_turn_detector_assets():
        logger.warning(
            "turn_detector_plugin_not_registered",
            extra={
                "reason": "multilingual turn-detector assets missing; ml mode will fall back to stt",
            },
        )
        return False

    try:
        from livekit.plugins.turn_detector import multilingual as _turn_detector_multilingual

        _ = _turn_detector_multilingual
        return True
    except Exception as exc:
        logger.warning(
            "turn_detector_plugin_registration_failed",
            extra={
                "reason": f"{type(exc).__name__}: {exc}"[:500],
                "remote_eot": bool(remote_eot_url),
            },
        )
        return False


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

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        del turn_ctx

        text = (new_message.text_content or "").strip()
        suppress, details = _should_suppress_fragment_turn(text)
        if not suppress:
            return

        logger.info(
            "suppressing_fragment_turn",
            extra={
                "transcript": text[:200],
                "normalized_text": details["normalized_text"],
                "word_count": details["word_count"],
                "char_count": details["char_count"],
                "reason": details["reason"],
            },
        )
        raise StopResponse()


TURN_DETECTOR_PLUGIN_READY = _register_turn_detector_plugin_if_available()


server = AgentServer(
    num_idle_processes=NUM_IDLE_PROCESSES,
    job_memory_warn_mb=JOB_MEMORY_WARN_MB,
    job_memory_limit_mb=JOB_MEMORY_LIMIT_MB,
    shutdown_process_timeout=SHUTDOWN_PROCESS_TIMEOUT_SEC,
)


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
    llm_model_override = str(voice_settings.get("llm_model", "")).strip().lower()

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
    resolved_llm_model = (
        llm_model_override
        if llm_model_override in ("zai-glm-4.6", "zai-glm-4.7")
        else LLM_MODEL
    )

    logger.info(
        "starting session",
        extra={
            "agent_name": AGENT_NAME,
            "stt_model": STT_MODEL,
            "stt_language": STT_LANGUAGE,
            "llm_model": resolved_llm_model,
            "tts_model": TTS_MODEL,
            "tts_voice": resolved_tts_voice or None,
            "turn_detection": turn_detection_mode,
            "session_kwargs": session_kwargs_log,
            "fragment_turn_suppression": {
                "enabled": SUPPRESS_FRAGMENT_TURNS,
                "max_words": FRAGMENT_TURN_MAX_WORDS,
                "max_chars": FRAGMENT_TURN_MAX_CHARS,
                "allowlist_size": len(FRAGMENT_TURN_ALLOWLIST),
                "blocklist_size": len(FRAGMENT_TURN_BLOCKLIST),
            },
        },
    )

    tts_kwargs = {"model": TTS_MODEL}
    if resolved_tts_voice:
        tts_kwargs["voice"] = resolved_tts_voice

    session = AgentSession(
        # STT: Cartesia Ink Whisper
        stt=cartesia.STT(model=STT_MODEL, language=STT_LANGUAGE),
        # LLM: Cerebras Cloud (OpenAI-compatible) — e.g. "zai-glm-4.6" or "zai-glm-4.7"
        llm=openai.LLM.with_cerebras(model=resolved_llm_model),
        # TTS: Cartesia Sonic
        tts=cartesia.TTS(**tts_kwargs),
        **session_kwargs,
    )
    voice_agent = VoiceAgent()
    context_lock = asyncio.Lock()
    last_llm_failure_announcement_at = 0.0
    room_cleanup_task: asyncio.Task[None] | None = None
    session_started_at = time.monotonic()
    last_activity_at = time.monotonic()
    idle_timeout_triggered = False
    last_final_user_transcript_key: str | None = None
    last_final_user_transcript_speaker: str | None = None
    last_final_user_transcript_at = 0.0

    def touch_activity() -> None:
        nonlocal last_activity_at
        last_activity_at = time.monotonic()

    def reset_final_transcript_dedupe() -> None:
        nonlocal last_final_user_transcript_key
        nonlocal last_final_user_transcript_speaker
        nonlocal last_final_user_transcript_at
        last_final_user_transcript_key = None
        last_final_user_transcript_speaker = None
        last_final_user_transcript_at = 0.0

    def is_duplicate_final_transcript(ev: voice_events.UserInputTranscribedEvent) -> bool:
        nonlocal last_final_user_transcript_key
        nonlocal last_final_user_transcript_speaker
        nonlocal last_final_user_transcript_at

        if not ev.is_final or STT_FINAL_DEDUP_WINDOW_SEC <= 0:
            return False

        transcript_key = _normalize_transcript_key(ev.transcript)
        if not transcript_key:
            return False

        speaker_key = str(ev.speaker_id or "").strip() or None
        created_at = float(getattr(ev, "created_at", 0.0) or time.time())
        is_duplicate = (
            transcript_key == last_final_user_transcript_key
            and speaker_key == last_final_user_transcript_speaker
            and created_at >= last_final_user_transcript_at
            and created_at - last_final_user_transcript_at <= STT_FINAL_DEDUP_WINDOW_SEC
        )

        last_final_user_transcript_key = transcript_key
        last_final_user_transcript_speaker = speaker_key
        last_final_user_transcript_at = created_at
        return is_duplicate

    def log_session_metric(event: str, **extra: object) -> None:
        room_name = str(getattr(ctx.room, "name", "") or "").strip() or None
        logger.info(
            "session_metric",
            extra={
                "event": event,
                "room": room_name,
                "session_age_sec": round(time.monotonic() - session_started_at, 1),
                "idle_for_sec": round(time.monotonic() - last_activity_at, 1),
                **extra,
            },
        )

    async def publish_debug_event(payload: dict[str, object], *, reliable: bool = True) -> None:
        if not DEBUG_DATA_ENABLED or not DEBUG_DATA_TOPIC:
            return
        try:
            payload_with_meta = {
                **payload,
                "ts": time.time(),
                "agent_name": AGENT_NAME,
                "llm_model": resolved_llm_model,
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

    def schedule_room_cleanup(
        trigger_reason: str,
        *,
        delay: float = 0.0,
        force: bool = False,
    ) -> None:
        nonlocal room_cleanup_task

        room_name = str(getattr(ctx.room, "name", "") or "").strip()
        if not room_name:
            return

        if room_cleanup_task and not room_cleanup_task.done():
            return

        log_session_metric(
            "cleanup_scheduled",
            trigger_reason=trigger_reason,
            delay_sec=delay,
            force=force,
        )

        async def run_room_cleanup() -> None:
            if delay > 0:
                await asyncio.sleep(delay)
            deleted = await _cleanup_room_if_empty(
                room_name,
                trigger_reason=trigger_reason,
                force=force,
            )
            log_session_metric(
                "cleanup_result",
                trigger_reason=trigger_reason,
                deleted=deleted,
                force=force,
            )
            if deleted:
                try:
                    ctx.shutdown(f"empty_room_cleanup:{trigger_reason}")
                except Exception:
                    logger.exception(
                        "Failed to request shutdown after empty-room cleanup",
                        extra={"room": room_name, "trigger_reason": trigger_reason},
                    )

        room_cleanup_task = asyncio.create_task(
            run_room_cleanup(),
            name="cleanup_room_if_empty",
        )

    async def announce_llm_failure() -> None:
        nonlocal last_llm_failure_announcement_at

        now = time.monotonic()
        if now - last_llm_failure_announcement_at < LLM_ERROR_SPOKEN_COOLDOWN_SEC:
            return

        last_llm_failure_announcement_at = now
        message = _default_llm_failure_message(resolved_llm_model)

        try:
            await session.say(message, allow_interruptions=True, add_to_chat_ctx=False)
        except Exception:
            logger.exception(
                "failed to speak llm failure notice",
                extra={"llm_model": resolved_llm_model},
            )

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

        # Accept legacy clients that can't set LiveKit topics and publish on the empty/default topic.
        # We still prefer the explicit CONTROL_DATA_TOPIC when available to avoid collisions.
        topic = str(getattr(packet, "topic", "") or "").strip()
        if topic != CONTROL_DATA_TOPIC:
            if topic == "" and "owui.voice.control" in raw:
                logger.warning(
                    "Control payload received without topic; accepting for compatibility",
                    extra={"expected_topic": CONTROL_DATA_TOPIC, "identity": sender_identity},
                )
            else:
                # If this *looks* like a control payload but was published on the wrong topic, log it.
                if "owui.voice.control" in raw:
                    logger.warning(
                        "Control payload received on wrong topic",
                        extra={"topic": topic, "identity": sender_identity},
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
            touch_activity()
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
            touch_activity()
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
        touch_activity()
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
        if ev.new_state == "speaking":
            reset_final_transcript_dedupe()
        schedule_publish(
            {
                "type": "owui.voice.event",
                "event": "user_state_changed",
                "data": ev.model_dump(),
            }
        )

    def on_user_input_transcribed(ev: voice_events.UserInputTranscribedEvent) -> None:
        touch_activity()
        # Emitted for both partial and final transcripts.
        if ev.is_final:
            if is_duplicate_final_transcript(ev):
                logger.debug(
                    "user_input_transcribed_final_duplicate",
                    extra={
                        "language": ev.language,
                        "speaker_id": ev.speaker_id,
                        "transcript": (ev.transcript or "")[:500],
                    },
                )
                return
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
        touch_activity()
        # Only send a compact view to avoid huge payloads.
        item = ev.item
        payload: dict[str, object] = {
            "type": "owui.voice.event",
            "event": "conversation_item_added",
        }
        if getattr(item, "role", None) == "assistant":
            reset_final_transcript_dedupe()
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
        touch_activity()
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

        try:
            is_llm_error = getattr(err, "type", None) == "llm_error"
            recoverable = bool(getattr(err, "recoverable", False))
        except Exception:
            is_llm_error = False
            recoverable = False

        if is_llm_error and not recoverable:
            logger.warning(
                "nonrecoverable_llm_error",
                extra={
                    "llm_model": resolved_llm_model,
                    "error": str(exc) if exc is not None else str(err),
                },
            )
            asyncio.create_task(announce_llm_failure(), name="announce_llm_failure")

    def on_function_tools_executed(ev: voice_events.FunctionToolsExecutedEvent) -> None:
        touch_activity()
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
        touch_activity()
        logger.warning(
            "session_close",
            extra={"reason": ev.reason, "error": str(ev.error) if ev.error else None},
        )
        log_session_metric(
            "close",
            reason=str(ev.reason),
            error=str(ev.error) if ev.error else None,
        )
        schedule_room_cleanup(str(ev.reason))
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

    def on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
        touch_activity()
        identity = str(getattr(participant, "identity", "") or "").strip()
        if identity and not identity.startswith("agent-"):
            log_session_metric(
                "participant_disconnected",
                participant_identity=identity,
            )
            schedule_room_cleanup(
                f"participant_disconnected:{identity}",
                delay=0.75,
            )

    async def idle_watchdog() -> None:
        nonlocal idle_timeout_triggered

        room_name = str(getattr(ctx.room, "name", "") or "").strip()
        if (
            SESSION_IDLE_TIMEOUT_SEC <= 0
            or SESSION_IDLE_CHECK_INTERVAL_SEC <= 0
            or not room_name
        ):
            return

        try:
            while True:
                await asyncio.sleep(SESSION_IDLE_CHECK_INTERVAL_SEC)
                idle_for = time.monotonic() - last_activity_at
                if idle_for < SESSION_IDLE_TIMEOUT_SEC or idle_timeout_triggered:
                    continue

                idle_timeout_triggered = True
                log_session_metric(
                    "idle_timeout",
                    timeout_sec=SESSION_IDLE_TIMEOUT_SEC,
                    force_delete=SESSION_IDLE_FORCE_DELETE,
                )
                logger.warning(
                    "session idle timeout exceeded",
                    extra={
                        "room": room_name,
                        "idle_for_sec": round(idle_for, 1),
                        "timeout_sec": SESSION_IDLE_TIMEOUT_SEC,
                        "force_delete": SESSION_IDLE_FORCE_DELETE,
                    },
                )
                try:
                    await _cleanup_room_if_empty(
                        room_name,
                        trigger_reason="idle_timeout",
                        force=SESSION_IDLE_FORCE_DELETE,
                    )
                finally:
                    try:
                        ctx.shutdown(f"idle_timeout:{int(idle_for)}")
                    except Exception:
                        logger.exception(
                            "Failed to request shutdown after idle timeout",
                            extra={"room": room_name},
                        )
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Idle watchdog failed", extra={"room": room_name})

    schedule_publish(
        {
            "type": "owui.voice.event",
            "event": "agent_starting",
            "data": {
                "turn_detection": turn_detection_mode,
                "session_kwargs": session_kwargs_log,
                "tts_voice": resolved_tts_voice or None,
                "llm_model": resolved_llm_model,
                "stt_model": STT_MODEL,
                "stt_language": STT_LANGUAGE,
                "tts_model": TTS_MODEL,
                "fragment_turn_suppression": {
                    "enabled": SUPPRESS_FRAGMENT_TURNS,
                    "max_words": FRAGMENT_TURN_MAX_WORDS,
                    "max_chars": FRAGMENT_TURN_MAX_CHARS,
                },
            },
        }
    )

    ctx.room.on("participant_disconnected", on_participant_disconnected)
    ctx.room.on("data_received", on_data_received)
    await session.start(
        room=ctx.room,
        agent=voice_agent,
        room_input_options=room_io.RoomInputOptions(close_on_disconnect=True),
    )
    asyncio.create_task(idle_watchdog(), name="session_idle_watchdog")
    touch_activity()
    log_session_metric(
        "started",
        turn_detection=turn_detection_mode,
        llm_model=resolved_llm_model,
        tts_voice=resolved_tts_voice or None,
    )
    if STARTUP_MESSAGE:
        try:
            await session.say(STARTUP_MESSAGE, add_to_chat_ctx=False)
            touch_activity()
        except Exception:
            logger.exception("startup TTS failed")


if __name__ == "__main__":
    agents.cli.run_app(server)
