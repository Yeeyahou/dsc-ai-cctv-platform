import json
from pathlib import Path

from dsc.backend.api import create_app
from dsc.backend.config import Settings
from dsc.backend.service import Platform


def main() -> None:
    app = create_app(
        Platform(None, Settings(environment="test"), None, None), verifier=lambda _: {}
    )
    schema = app.openapi()
    schema.setdefault("components", {})["securitySchemes"] = {
        "CognitoAccessToken": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    }
    schema["security"] = [{"CognitoAccessToken": []}]
    Path("docs/openapi.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
