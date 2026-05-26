import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

import jwt
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

LIVEKIT_DIR = Path(__file__).resolve().parent
HTML_PATH = LIVEKIT_DIR / "index.html"
MANIFEST_PATH = LIVEKIT_DIR / "manifest.webmanifest"
SW_PATH = LIVEKIT_DIR / "sw.js"
ICONS_DIR = LIVEKIT_DIR / "icons"
KEY_FILE = Path.cwd() / ".webui_secret_key"

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
LIVEKIT_HTTP_URL = os.environ.get("LIVEKIT_HTTP_URL", "http://127.0.0.1:7880").strip()
WEBUI_AUTH_URL = os.environ.get("WEBUI_AUTH_URL", "http://127.0.0.1:8080/api/v1/auths/").strip()

AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "owui-voice")
ROOM_PREFIX = os.getenv("LIVEKIT_ROOM_PREFIX", "owui-voice")
ROOM_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

logger = logging.getLogger("uvicorn.error")


def _require_env(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _get_webui_secret_key() -> str:
    secret = os.environ.get("WEBUI_SECRET_KEY", "").strip()
    if secret:
        return secret

    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip()

    return ""


def _get_livekit():
    from livekit import api

    return api


def _request_remote_addr(request: Request | None) -> str | None:
    return getattr(getattr(request, "client", None), "host", None)


def _request_user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    return str(request.headers.get("user-agent", "") or "").strip()[:512] or None


def _log_portal_metric(
    event: str,
    *,
    request: Request | None = None,
    user_id: str | None = None,
    room: str | None = None,
    **extra: object,
) -> None:
    payload: dict[str, object] = {
        "event": event,
        "user_id": user_id,
        "room": room,
        "remote_addr": _request_remote_addr(request),
        "user_agent": _request_user_agent(request),
        **extra,
    }
    print(
        "portal_metric",
        json.dumps(
            {key: value for key, value in payload.items() if value is not None},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
        flush=True,
    )


def _get_auth_candidates(
    request: Request,
    authorization: str | None,
) -> list[str]:
    candidates: list[str] = []

    if authorization and authorization.startswith("Bearer "):
        auth_token = authorization.removeprefix("Bearer ").strip()
        if auth_token:
            candidates.append(auth_token)

    cookie_token = str(request.cookies.get("token", "") or "").strip()
    if cookie_token and cookie_token not in candidates:
        candidates.append(cookie_token)

    return candidates


def _decode_webui_jwt(auth_token: str) -> dict[str, Any] | None:
    if not auth_token or auth_token.startswith("sk-"):
        return None

    webui_secret_key = _get_webui_secret_key()
    if not webui_secret_key:
        return None

    try:
        decoded = jwt.decode(auth_token, webui_secret_key, algorithms=["HS256"])
    except Exception:
        return None

    return decoded if isinstance(decoded, dict) and "id" in decoded else None


def _lookup_openwebui_user(auth_token: str) -> dict[str, Any] | None:
    if not auth_token:
        return None

    request = urllib_request.Request(
        WEBUI_AUTH_URL,
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib_request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError:
        return None
    except urllib_error.URLError:
        logger.exception("Failed to reach Open WebUI auth endpoint")
        return None
    except Exception:
        logger.exception("Failed to validate Open WebUI auth token")
        return None

    return payload if isinstance(payload, dict) and "id" in payload else None


def _authenticate_openwebui_request(
    request: Request,
    authorization: str | None,
) -> str:
    candidates = _get_auth_candidates(request, authorization)
    if not candidates:
        _log_portal_metric("auth_missing", request=request)
        raise HTTPException(status_code=401, detail="Missing Open WebUI auth token")

    for auth_token in candidates:
        decoded = _decode_webui_jwt(auth_token)
        if decoded:
            return str(decoded["id"])

    for auth_token in candidates:
        payload = _lookup_openwebui_user(auth_token)
        if payload:
            return str(payload["id"])

    _log_portal_metric("auth_invalid", request=request, token_candidates=len(candidates))
    raise HTTPException(status_code=401, detail="Invalid Open WebUI token")


def _build_voice_settings(
    *,
    llm_model: str | None,
    turn_detection: str | None,
    allow_interruptions: bool | None,
    min_endpointing_delay: float | None,
    max_endpointing_delay: float | None,
    min_interruption_duration: float | None,
    min_interruption_words: int | None,
    tts_voice: str | None,
    web_search: bool | None,
) -> dict[str, object]:
    voice_settings: dict[str, object] = {}

    if llm_model is not None:
        normalized_llm_model = llm_model.strip()
        if normalized_llm_model:
            lowered = normalized_llm_model.lower()
            if lowered in ("4.6", "glm4.6", "glm-4.6", "zai-glm-4.6"):
                voice_settings["llm_model"] = "zai-glm-4.6"
            elif lowered in ("4.7", "glm4.7", "glm-4.7", "zai-glm-4.7"):
                voice_settings["llm_model"] = "zai-glm-4.7"
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid llm_model (allowed: glm-4.6, glm-4.7)",
                )

    if turn_detection:
        normalized_turn_detection = turn_detection.strip().lower()
        if normalized_turn_detection in ("ml", "multilingual"):
            voice_settings["turn_detection"] = "ml"
        elif normalized_turn_detection == "stt":
            voice_settings["turn_detection"] = "stt"
        else:
            raise HTTPException(
                status_code=400,
                detail="Invalid turn_detection (allowed: stt, ml)",
            )

    if allow_interruptions is not None:
        voice_settings["allow_interruptions"] = allow_interruptions
    if min_endpointing_delay is not None:
        voice_settings["min_endpointing_delay"] = min_endpointing_delay
    if max_endpointing_delay is not None:
        voice_settings["max_endpointing_delay"] = max_endpointing_delay
    if min_interruption_duration is not None:
        voice_settings["min_interruption_duration"] = min_interruption_duration
    if min_interruption_words is not None:
        voice_settings["min_interruption_words"] = min_interruption_words
    if tts_voice is not None:
        normalized_tts_voice = tts_voice.strip()
        if normalized_tts_voice:
            if len(normalized_tts_voice) > 256:
                raise HTTPException(status_code=400, detail="tts_voice is too long")
            voice_settings["tts_voice"] = normalized_tts_voice

    if web_search is not None:
        voice_settings["web_search"] = web_search

    if (
        min_endpointing_delay is not None
        and max_endpointing_delay is not None
        and min_endpointing_delay > max_endpointing_delay
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid endpointing delays (min_endpointing_delay > max_endpointing_delay)",
        )

    return voice_settings


app = FastAPI(title="Open WebUI LiveKit Portal")


class ClientLogEntry(BaseModel):
    ts: str | None = Field(default=None, description="Client timestamp (ISO string)")
    level: str = Field(description="Log level (INFO/WARN/ERROR/DEBUG)")
    msg: str = Field(description="Human-readable log message")
    data: Any | None = Field(default=None, description="Optional structured payload")


class ClientLogPayload(BaseModel):
    client_id: str | None = Field(default=None, max_length=128)
    room: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=512)
    output_mode: str | None = Field(default=None, max_length=32)
    reason: str | None = Field(default=None, max_length=64)
    entries: list[ClientLogEntry] = Field(default_factory=list)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(
        HTML_PATH.read_text(encoding="utf-8"),
        headers={
            # Avoid stale HTML in browsers/service workers; this page changes frequently while iterating.
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(
        MANIFEST_PATH,
        media_type="application/manifest+json",
        headers={
            # Keep install metadata fresh; changes should be picked up quickly.
            "Cache-Control": "no-cache",
        },
    )


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        SW_PATH,
        media_type="application/javascript",
        headers={
            # Service worker updates should always revalidate.
            "Cache-Control": "no-cache",
        },
    )


@app.post("/log")
async def client_log(
    payload: ClientLogPayload,
    request: Request,
    authorization: str | None = Header(default=None),
):
    user_id = _authenticate_openwebui_request(request, authorization)
    remote_addr = getattr(getattr(request, "client", None), "host", None)

    total = len(payload.entries or [])
    # Avoid unbounded log spam; the client batches and will flush again if needed.
    entries = list(payload.entries or [])[:200]

    base = {
        "user_id": user_id,
        "client_id": payload.client_id,
        "room": payload.room,
        "version": payload.version,
        "user_agent": payload.user_agent,
        "output_mode": payload.output_mode,
        "reason": payload.reason,
        "remote_addr": remote_addr,
    }

    for entry in entries:
        record = {
            **base,
            "ts": entry.ts,
            "level": entry.level,
            "msg": entry.msg,
            "data": entry.data,
        }
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        lvl = (entry.level or "").upper()
        if lvl in ("ERROR", "ERR"):
            logger.error("client_log %s", line)
        elif lvl in ("WARN", "WARNING"):
            logger.warning("client_log %s", line)
        elif lvl == "DEBUG":
            logger.debug("client_log %s", line)
        else:
            logger.info("client_log %s", line)

    return {"ok": True, "received": len(entries), "dropped": max(0, total - len(entries))}


@app.get("/icons/{filename}")
async def icon(filename: str):
    if filename not in ("icon-192.png", "icon-512.png"):
        raise HTTPException(status_code=404, detail="Not found")

    path = ICONS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")

    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
        },
    )


@app.get("/token")
async def token(
    request: Request,
    room: str | None = None,
    llm_model: str | None = Query(default=None),
    turn_detection: str | None = Query(default=None),
    allow_interruptions: bool | None = Query(default=None),
    min_endpointing_delay: float | None = Query(default=None, ge=0.0, le=10.0),
    max_endpointing_delay: float | None = Query(default=None, ge=0.0, le=10.0),
    min_interruption_duration: float | None = Query(default=None, ge=0.0, le=10.0),
    min_interruption_words: int | None = Query(default=None, ge=0, le=50),
    tts_voice: str | None = Query(default=None),
    web_search: bool | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    _require_env("LIVEKIT_URL", LIVEKIT_URL)
    _require_env("LIVEKIT_API_KEY", LIVEKIT_API_KEY)
    _require_env("LIVEKIT_API_SECRET", LIVEKIT_API_SECRET)

    user_id = _authenticate_openwebui_request(request, authorization)

    room_name = (room or "").strip()
    if not room_name:
        room_name = f"{ROOM_PREFIX}-{uuid.uuid4().hex}"
    elif not ROOM_NAME_RE.fullmatch(room_name) or not room_name.startswith(f"{ROOM_PREFIX}-"):
        raise HTTPException(status_code=400, detail="Invalid room name")

    api = _get_livekit()

    grants = api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )

    # Explicit agent dispatch: only dispatch the agent when Open WebUI mints a token.
    from livekit.protocol.agent_dispatch import RoomAgentDispatch
    from livekit.protocol.room import RoomConfiguration

    voice_settings = _build_voice_settings(
        llm_model=llm_model,
        turn_detection=turn_detection,
        allow_interruptions=allow_interruptions,
        min_endpointing_delay=min_endpointing_delay,
        max_endpointing_delay=max_endpointing_delay,
        min_interruption_duration=min_interruption_duration,
        min_interruption_words=min_interruption_words,
        tts_voice=tts_voice,
        web_search=web_search,
    )

    room_config = RoomConfiguration(agents=[RoomAgentDispatch(agent_name=AGENT_NAME)])
    metadata = (
        json.dumps({"owui_voice": voice_settings}, separators=(",", ":"), sort_keys=True)
        if voice_settings
        else ""
    )
    requested_room = bool((room or "").strip())

    # Always set metadata in the room config so newly-created rooms get the correct settings.
    # Note: RoomConfiguration metadata does NOT update existing rooms, so we also update it via RoomService below.
    room_config.metadata = metadata

    access_token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    access_token.with_identity(f"owui:{user_id}")
    access_token.with_name(f"Open WebUI User {user_id}")
    access_token.with_metadata(f'{{"open_webui_user_id":"{user_id}"}}')
    access_token.with_grants(grants)
    access_token.with_room_config(room_config)

    # Important: when you reuse an existing room name, LiveKit keeps the old room metadata.
    # That means "Turn Detection: STT-only" (or any other setting) can look "broken" because
    # the agent still sees the metadata from the first time the room was created.
    #
    # Fix: update the room metadata if the room already exists.
    try:
        lk = api.LiveKitAPI(
            url=LIVEKIT_HTTP_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )
        try:
            existing = await lk.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
            if existing.rooms:
                await lk.room.update_room_metadata(
                    api.UpdateRoomMetadataRequest(room=room_name, metadata=metadata)
                )

                # If the agent job is already running, it will not pick up the new room metadata.
                # Restart the dispatch when settings have changed so toggles (e.g. STT-only) actually apply.
                try:
                    from livekit.protocol import agent_dispatch as agent_dispatch_proto

                    dispatches = await lk.agent_dispatch.list_dispatch(room_name=room_name)
                    agent_dispatches = [
                        d for d in dispatches if getattr(d, "agent_name", None) == AGENT_NAME
                    ]

                    restart_needed = False
                    if not agent_dispatches:
                        restart_needed = True
                    else:
                        for d in agent_dispatches:
                            jobs = getattr(getattr(d, "state", None), "jobs", None) or []
                            for job in jobs:
                                job_room = getattr(job, "room", None)
                                job_metadata = getattr(job_room, "metadata", None) or ""
                                if job_metadata != metadata:
                                    restart_needed = True
                                    break
                            if restart_needed:
                                break

                    if restart_needed:
                        # Avoid disrupting an active room with non-agent participants.
                        participants = await lk.room.list_participants(
                            api.ListParticipantsRequest(room=room_name)
                        )
                        has_non_agent = any(
                            not str(getattr(p, "identity", "")).startswith("agent-")
                            for p in (participants.participants or [])
                        )
                        if has_non_agent:
                            _log_portal_metric(
                                "token_dispatch_restart_skipped",
                                request=request,
                                user_id=user_id,
                                room=room_name,
                                participant_count=len(participants.participants or []),
                                settings=voice_settings,
                            )
                            logger.warning(
                                "Skipping agent dispatch restart (room has active non-agent participants)",
                                extra={"room": room_name},
                            )
                        else:
                            for d in agent_dispatches:
                                await lk.agent_dispatch.delete_dispatch(d.id, room_name)
                            await lk.agent_dispatch.create_dispatch(
                                agent_dispatch_proto.CreateAgentDispatchRequest(
                                    agent_name=AGENT_NAME, room=room_name, metadata=""
                                )
                            )
                except Exception:
                    logger.exception("Failed to restart agent dispatch", extra={"room": room_name})
        finally:
            await lk.aclose()
    except Exception:
        logger.exception(
            "Failed to update LiveKit room metadata",
            extra={"room": room_name, "livekit_http_url": LIVEKIT_HTTP_URL},
        )

    _log_portal_metric(
        "token_issued",
        request=request,
        user_id=user_id,
        room=room_name,
        requested_room=requested_room,
        settings=voice_settings,
        metadata_chars=len(metadata),
    )
    return {
        "url": LIVEKIT_URL,
        "room": room_name,
        "token": access_token.to_jwt(),
        "identity": f"owui:{user_id}",
    }


@app.post("/apply")
async def apply(
    request: Request,
    room: str,
    llm_model: str | None = Query(default=None),
    turn_detection: str | None = Query(default=None),
    allow_interruptions: bool | None = Query(default=None),
    min_endpointing_delay: float | None = Query(default=None, ge=0.0, le=10.0),
    max_endpointing_delay: float | None = Query(default=None, ge=0.0, le=10.0),
    min_interruption_duration: float | None = Query(default=None, ge=0.0, le=10.0),
    min_interruption_words: int | None = Query(default=None, ge=0, le=50),
    tts_voice: str | None = Query(default=None),
    web_search: bool | None = Query(default=None),
    force: bool = Query(default=False),
    authorization: str | None = Header(default=None),
):
    _require_env("LIVEKIT_URL", LIVEKIT_URL)
    _require_env("LIVEKIT_API_KEY", LIVEKIT_API_KEY)
    _require_env("LIVEKIT_API_SECRET", LIVEKIT_API_SECRET)

    user_id = _authenticate_openwebui_request(request, authorization)

    room_name = (room or "").strip()
    if not room_name:
        raise HTTPException(status_code=400, detail="Room is required")
    if not ROOM_NAME_RE.fullmatch(room_name) or not room_name.startswith(f"{ROOM_PREFIX}-"):
        raise HTTPException(status_code=400, detail="Invalid room name")

    voice_settings = _build_voice_settings(
        llm_model=llm_model,
        turn_detection=turn_detection,
        allow_interruptions=allow_interruptions,
        min_endpointing_delay=min_endpointing_delay,
        max_endpointing_delay=max_endpointing_delay,
        min_interruption_duration=min_interruption_duration,
        min_interruption_words=min_interruption_words,
        tts_voice=tts_voice,
        web_search=web_search,
    )
    metadata = (
        json.dumps({"owui_voice": voice_settings}, separators=(",", ":"), sort_keys=True)
        if voice_settings
        else ""
    )

    api = _get_livekit()
    try:
        lk = api.LiveKitAPI(
            url=LIVEKIT_HTTP_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )
        try:
            existing = await lk.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
            if not existing.rooms:
                _log_portal_metric(
                    "apply_room_not_found",
                    request=request,
                    user_id=user_id,
                    room=room_name,
                    settings=voice_settings,
                )
                raise HTTPException(status_code=404, detail="Room not found")

            await lk.room.update_room_metadata(
                api.UpdateRoomMetadataRequest(room=room_name, metadata=metadata)
            )

            participants = await lk.room.list_participants(api.ListParticipantsRequest(room=room_name))
            other_humans = [
                str(getattr(p, "identity", ""))
                for p in (participants.participants or [])
                if not str(getattr(p, "identity", "")).startswith("agent-")
                and str(getattr(p, "identity", "")) != f"owui:{user_id}"
            ]
            if other_humans and not force:
                _log_portal_metric(
                    "apply_conflict",
                    request=request,
                    user_id=user_id,
                    room=room_name,
                    other_participants=other_humans,
                    force=force,
                    settings=voice_settings,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Room has other participants; pass force=true to restart the agent anyway",
                )

            # Restart the dispatch so the already-running job picks up the new metadata.
            try:
                from livekit.protocol import agent_dispatch as agent_dispatch_proto

                dispatches = await lk.agent_dispatch.list_dispatch(room_name=room_name)
                for d in dispatches:
                    if getattr(d, "agent_name", None) == AGENT_NAME:
                        await lk.agent_dispatch.delete_dispatch(d.id, room_name)

                await lk.agent_dispatch.create_dispatch(
                    agent_dispatch_proto.CreateAgentDispatchRequest(
                        agent_name=AGENT_NAME, room=room_name, metadata=""
                    )
                )
            except Exception:
                logger.exception("Failed to restart agent dispatch", extra={"room": room_name})
                raise HTTPException(status_code=500, detail="Failed to restart agent dispatch")
        finally:
            await lk.aclose()
    except HTTPException:
        raise
    except Exception:
        logger.exception("Apply failed", extra={"room": room_name})
        raise HTTPException(status_code=500, detail="Apply failed")

    _log_portal_metric(
        "apply_ok",
        request=request,
        user_id=user_id,
        room=room_name,
        force=force,
        settings=voice_settings,
        metadata_chars=len(metadata),
    )
    return {"ok": True, "room": room_name, "metadata": metadata}


@app.post("/leave")
async def leave(
    request: Request,
    room: str,
    authorization: str | None = Header(default=None),
):
    _require_env("LIVEKIT_URL", LIVEKIT_URL)
    _require_env("LIVEKIT_API_KEY", LIVEKIT_API_KEY)
    _require_env("LIVEKIT_API_SECRET", LIVEKIT_API_SECRET)

    user_id = _authenticate_openwebui_request(request, authorization)

    room_name = (room or "").strip()
    if not room_name:
        raise HTTPException(status_code=400, detail="Room is required")
    if not ROOM_NAME_RE.fullmatch(room_name) or not room_name.startswith(f"{ROOM_PREFIX}-"):
        raise HTTPException(status_code=400, detail="Invalid room name")

    api = _get_livekit()
    requester_identity = f"owui:{user_id}"

    try:
        lk = api.LiveKitAPI(
            url=LIVEKIT_HTTP_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )
        try:
            existing = await lk.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
            if not existing.rooms:
                _log_portal_metric(
                    "leave_room_not_found",
                    request=request,
                    user_id=user_id,
                    room=room_name,
                )
                return {"ok": True, "room": room_name, "deleted": False, "reason": "room_not_found"}

            participants = await lk.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )
            other_humans = [
                str(getattr(p, "identity", ""))
                for p in (participants.participants or [])
                if not str(getattr(p, "identity", "")).startswith("agent-")
                and str(getattr(p, "identity", "")) != requester_identity
            ]

            if other_humans:
                _log_portal_metric(
                    "leave_other_participants",
                    request=request,
                    user_id=user_id,
                    room=room_name,
                    other_participants=other_humans,
                )
                return {
                    "ok": True,
                    "room": room_name,
                    "deleted": False,
                    "reason": "other_participants",
                    "participants": other_humans,
                }

            try:
                dispatches = await lk.agent_dispatch.list_dispatch(room_name=room_name)
                for d in dispatches:
                    if getattr(d, "agent_name", None) == AGENT_NAME:
                        await lk.agent_dispatch.delete_dispatch(d.id, room_name)
            except Exception:
                logger.exception(
                    "Failed to delete agent dispatch during leave cleanup",
                    extra={"room": room_name},
                )

            await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
            logger.info(
                "Deleted room during leave cleanup",
                extra={"room": room_name, "user_id": user_id},
            )
            _log_portal_metric(
                "leave_deleted",
                request=request,
                user_id=user_id,
                room=room_name,
            )
            return {"ok": True, "room": room_name, "deleted": True}
        finally:
            await lk.aclose()
    except HTTPException:
        raise
    except Exception:
        logger.exception("Leave cleanup failed", extra={"room": room_name})
        raise HTTPException(status_code=500, detail="Leave cleanup failed")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8091")))
