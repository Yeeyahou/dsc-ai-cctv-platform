"""Generate v1.3 live media and control diagrams using the retained diagram style."""

import argparse
from pathlib import Path

import render_architecture as drawing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path, required=True)
    args = parser.parse_args()
    drawing.FONT = args.font
    figure = drawing.Figure("live-media-v1.3", "실시간 영상 경로", width=1000, height=720)
    figure.node("camera", 340, 115, 320, 85, "현장 CCTV", ["RTSP H264"])
    figure.node("media", 340, 325, 320, 85, "현장 MediaMTX", ["시청 중에만 수신"])
    figure.node("browser", 340, 535, 320, 85, "사무실 브라우저", ["VPN 안의 시청자"])
    figure.edge(
        "L01",
        "camera",
        "media",
        [(500, 200), (500, 325)],
        "RTSP 영상 · 현장 LAN",
        (740, 270),
        color=drawing.ORANGE,
    )
    figure.edge(
        "L02",
        "media",
        "browser",
        [(500, 410), (500, 535)],
        "WebRTC 영상 · VPN",
        (740, 480),
        color=drawing.ORANGE,
    )
    control = drawing.sequence(
        "live-control-v1.3",
        "실시간 시청 권한과 연결 종료",
        [
            ("브라우저", "직원 계정"),
            ("Cognito", "회사 로그인"),
            ("AWS API", "권한·세션 DB"),
            ("현장 게이트웨이", "인증·MediaMTX"),
        ],
        [
            ("L03", 0, 1, "PKCE 로그인", "api"),
            ("L04", 1, 0, "access token", "return"),
            ("L05", 0, 2, "목록·시청 시작", "api"),
            ("L06", 2, 0, "시청 토큰·URL", "return"),
            ("L07", 0, 3, "WHEP·시청 토큰", "api"),
            ("L08", 3, 2, "M2M·토큰 확인", "api"),
            ("L09", 2, 3, "권한 확인 결과", "return"),
            ("L10", 3, 2, "주기적 세션 검사", "api"),
            ("L11", 2, 3, "활성 세션 목록", "return"),
            ("L12", 3, 3, "무효 연결 종료", "api"),
        ],
    )
    output = Path("docs/diagrams")
    for item in [figure, control]:
        item.save(output, validate=bool(item.edges))
        path = output / f"{item.name}.svg"
        path.write_text(
            path.read_text().replace("Nanum Gothic, sans-serif", "Apple SD Gothic Neo, sans-serif")
        )


if __name__ == "__main__":
    main()
