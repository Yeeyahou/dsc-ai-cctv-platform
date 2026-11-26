"""Generate a restricted MediaMTX configuration from explicit camera registrations."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from dsc.shared.observability import configure_logging, logged


class CameraSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    rtsp_url: str
    credentials_file: Path

    @field_validator("rtsp_url")
    @classmethod
    def source_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"rtsp", "rtsps"}
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
            or not url.path
            or "$" in value
        ):
            raise ValueError("a fixed RTSP camera URL without embedded credentials is required")
        return value


class Installation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vpn_address: ipaddress.IPv4Address
    browser_origin: str
    cameras: list[CameraSource] = Field(min_length=1, max_length=32)

    @field_validator("browser_origin")
    @classmethod
    def browser(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("browser_origin must be an HTTPS origin")
        return value.rstrip("/")


class CameraCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: SecretStr = Field(min_length=1)
    password: SecretStr = Field(min_length=1)


@logged()
def media_config(installation: Installation) -> dict:
    if installation.vpn_address.is_unspecified or installation.vpn_address.is_multicast:
        raise ValueError("a reachable VPN interface address is required")
    paths = {}
    for camera in installation.cameras:
        if camera.path in paths:
            raise ValueError("duplicate stream path")
        if camera.credentials_file.stat().st_mode & 0o077:
            raise PermissionError("RTSP credentials must have mode 0600")
        credentials = CameraCredentials.model_validate_json(camera.credentials_file.read_text())
        url = urlsplit(camera.rtsp_url)
        user = quote(credentials.username.get_secret_value(), safe="")
        password = quote(credentials.password.get_secret_value(), safe="")
        source = url._replace(netloc=f"{user}:{password}@{url.netloc}").geturl()
        paths[camera.path] = {
            "source": source,
            "sourceOnDemand": True,
            "sourceOnDemandStartTimeout": "10s",
            "sourceOnDemandCloseAfter": "5s",
            "rtspTransport": "tcp",
            "maxReaders": 4,
            "record": False,
        }
    return {
        "logLevel": "error",
        "logStructured": True,
        "readTimeout": "10s",
        "writeTimeout": "10s",
        "authMethod": "http",
        "authHTTPAddress": "http://127.0.0.1:8090/internal/authorize",
        "authHTTPExclude": [{"action": "api"}],
        "api": True,
        "apiAddress": "127.0.0.1:9997",
        "metrics": False,
        "pprof": False,
        "playback": False,
        "rtsp": False,
        "rtmp": False,
        "hls": False,
        "srt": False,
        "moq": False,
        "webrtc": True,
        "webrtcAddress": "127.0.0.1:8889",
        "webrtcAllowOrigins": [installation.browser_origin],
        "webrtcLocalUDPAddress": ":8189",
        "webrtcLocalTCPAddress": ":8189",
        "webrtcIPsFromInterfaces": False,
        "webrtcAdditionalHosts": [str(installation.vpn_address)],
        "paths": paths,
    }


def main() -> int:
    configure_logging("live-install")
    parser = argparse.ArgumentParser(description="Generate private MediaMTX configuration")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        config = media_config(Installation.model_validate_json(args.manifest.read_text()))
        # Refuse overwrites: stop the service and explicitly move the old config when rotating.
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except (ValueError, OSError):
        print("설정 생성 실패: 형식·경로·파일 권한을 확인하세요. 비밀 원문은 출력하지 않습니다.")
        return 1
    print("MediaMTX 비공개 설정 생성 완료")
    return 0
