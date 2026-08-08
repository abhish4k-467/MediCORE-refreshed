import logging
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from redis import Redis
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.services.nl_query import NaturalLanguageQueryEngine
from backend.app.services.llm import is_token_limit_error

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/ws/chat")
async def chat_socket(websocket: WebSocket, db: Session = Depends(get_db)) -> None:
    await websocket.accept()
    
    # Extract and verify token query parameter for secure tenant-isolation
    token = websocket.query_params.get("token")
    if token:
        token = token.strip('"\'')
    
    tenant_id = None
    user_id = None
    if token and len(token.split('.')) == 3:
        try:
            from uuid import UUID
            from backend.app.db import get_supabase
            from backend.app.models import Profile
            supabase_client = get_supabase()
            response = supabase_client.auth.get_user(token)
            if response and response.user:
                user_uuid = UUID(response.user.id)
                user_id = user_uuid
                # A profile's tenant_id links an employee to an organisation;
                # chat/catalogue data remains private to the employee account.
                profile = db.query(Profile).filter(Profile.id == user_uuid).first()
                if not profile:
                    await websocket.send_json({"type": "error", "message": "Authentication failed. Missing profile identity. Connection closed."})
                    await websocket.close()
                    return
                tenant_id = user_uuid
        except Exception:
            logger.exception("WebSocket authentication failed with exception")
            await websocket.send_json({"type": "error", "message": "Authentication failed. Connection closed."})
            await websocket.close()
            return
    else:
        logger.warning("WebSocket connection attempt without a valid token format rejected")
        await websocket.send_json({"type": "error", "message": "Authentication failed. Missing or invalid token format. Connection closed."})
        await websocket.close()
        return

    if not tenant_id or not user_id:
        logger.warning("WebSocket connection attempt failed authentication (tenant_id/user_id could not be resolved) rejected")
        await websocket.send_json({"type": "error", "message": "Authentication failed. Missing profile identity. Connection closed."})
        await websocket.close()
        return

    settings = get_settings()
    cache = Redis.from_url(settings.queue_url, decode_responses=True)
    engine = NaturalLanguageQueryEngine(db=db, cache=cache)

    try:
        while True:
            message = await websocket.receive_text()
            try:
                result = engine.answer(message, tenant_id=tenant_id, user_id=user_id)
                await websocket.send_json(jsonable_encoder({"type": "answer", "answer": result.answer, "rows": result.rows}))
            except Exception as exc:
                logger.exception("Chat query failed for tenant_id=%s user_id=%s message=%r: %s", tenant_id, user_id, message[:500], exc)
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": "Token Limit Reached" if is_token_limit_error(exc) else "MediCORE could not complete that query.",
                    }
                )
    except WebSocketDisconnect:
        return
