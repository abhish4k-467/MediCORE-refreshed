import secrets

from fastapi import APIRouter, Header, HTTPException, Request

from backend.app.config import get_settings
from backend.app.tasks import process_gmail_notification

router = APIRouter()


@router.post("/gmail")
async def gmail_push(
    request: Request,
    x_webhook_token: str | None = Header(default=None),
) -> dict[str, str]:
    settings = get_settings()
    if not settings.gmail_webhook_token:
        raise HTTPException(status_code=503, detail="Webhook is not configured")
    if not x_webhook_token or not secrets.compare_digest(x_webhook_token, settings.gmail_webhook_token):
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    payload = await request.json()
    task = process_gmail_notification.delay(payload)
    return {"status": "queued", "task_id": task.id}
