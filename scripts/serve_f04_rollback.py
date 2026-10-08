"""Lance le code de retour dans un processus unique, entrées Telegram toujours fermées."""

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class TelegramMaintenance:
    """Aucune commande/callback ancien ne peut activer une demande gelée."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith(
            "/api/v1/telegram/webhook/"
        ):
            await PlainTextResponse("Maintenance Telegram", status_code=503)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def main() -> None:
    import uvicorn

    from app.main import app

    uvicorn.run(TelegramMaintenance(app), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
