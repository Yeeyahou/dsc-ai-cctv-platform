"""Render the documented architecture as editable SVGs; no application code is changed.

Authoring dependency: Pillow and a Korean TrueType font. Rasterize the SVGs with
Sharp or another SVG renderer after running this script. See the architecture guide.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from html import escape
from pathlib import Path

from PIL import ImageFont

BLUE = "#235D92"
ORANGE = "#A44F11"
PURPLE = "#704892"
GRAY = "#586575"
RED = "#AD3333"
INK = "#172431"


@dataclass
class Box:
    name: str
    x: float
    y: float
    w: float
    h: float


class Figure:
    def __init__(self, name, title, subtitle, width=2400, height=1760):
        self.name, self.width, self.height = name, width, height
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" role="img">',
            f"<title>{escape(title)}</title><desc>{escape(subtitle)}</desc>",
            '<rect width="100%" height="100%" fill="white"/>',
        ]
        self.nodes = {}
        self.edges = []
        self.labels = []
        self.text(45, 48, title, 34, bold=True, anchor="start")
        self.text(45, 91, subtitle, 23, anchor="start", color=GRAY)

    def text(self, x, y, text, size=24, *, bold=False, anchor="middle", color=INK):
        font = ImageFont.truetype(str(FONT), size)
        width = font.getlength(text)
        left = x if anchor == "start" else x - width / 2 if anchor == "middle" else x - width
        assert left >= 0 and left + width <= self.width, (self.name, text, left, width)
        self.parts.append(
            f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
            f'font-family="Nanum Gothic, sans-serif" font-size="{size}" '
            f'font-weight="{700 if bold else 400}" fill="{color}">{escape(text)}</text>'
        )
        return (left, y - size, width, size + 4)

    def panel(self, x, y, w, h, title, fill="#F7F9FB"):
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" '
            f'fill="{fill}" stroke="#A3ADB8" stroke-width="2" stroke-dasharray="10 7"/>'
        )
        bounds = self.text(x + 22, y + 39, title, 27, bold=True, anchor="start")
        self.labels.append(("boundary:" + title, bounds))

    def node(self, name, x, y, w, h, title, lines=(), *, color=BLUE, size=24):
        self.nodes[name] = Box(name, x, y, w, h)
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" '
            f'fill="white" stroke="{color}" stroke-width="2.4"/>'
        )
        headings = wrap(title, w - 28, size + 3)
        for index, heading in enumerate(headings):
            self.text(x + w / 2, y + 39 + (size + 9) * index, heading, size + 3, bold=True)
        body = [part for line in lines for part in wrap(line, w - 28, size)]
        body_start = 77 + (size + 9) * (len(headings) - 1)
        line_height = min(42, (h - body_start - 9) / max(len(body) - 1, 1))
        assert line_height >= size + 3 or len(body) == 1, (name, "increase box height", body)
        for index, line in enumerate(body):
            box = self.text(x + w / 2, y + body_start + line_height * index, line, size)
            assert box[0] >= x + 10 and box[0] + box[2] <= x + w - 10, (name, line)
            assert box[1] + box[3] <= y + h - 5, (name, line)

    def edge(self, identity, source, target, points, label, label_at, *, color=BLUE, dash=False):
        assert source in self.nodes and target in self.nodes
        start, end = points[0], points[-1]
        for p, node in ((start, self.nodes[source]), (end, self.nodes[target])):
            x, y = p
            assert (x in {node.x, node.x + node.w} and node.y <= y <= node.y + node.h) or (
                y in {node.y, node.y + node.h} and node.x <= x <= node.x + node.w
            ), (identity, p, node)
        self.arrow(points, color=color, dash=dash)
        self.edges.append((identity, source, target, points))
        bounds = self.text(*label_at, f"{identity}  {label}", 21, color=color)
        self.labels.append((identity, bounds))

    def arrow(self, points, *, color=BLUE, dash=False):
        value = " ".join(f"{x},{y}" for x, y in points)
        self.parts.append(
            f'<polyline points="{value}" fill="none" stroke="{color}" stroke-width="2.6" '
            f"{'stroke-dasharray="9 6"' if dash else ''}/>"
        )
        x, y = points[-1]
        px, py = points[-2]
        if x > px:
            triangle = [(x, y), (x - 12, y - 5), (x - 12, y + 5)]
        elif x < px:
            triangle = [(x, y), (x + 12, y - 5), (x + 12, y + 5)]
        elif y > py:
            triangle = [(x, y), (x - 5, y - 12), (x + 5, y - 12)]
        else:
            triangle = [(x, y), (x - 5, y + 12), (x + 5, y + 12)]
        value = " ".join(f"{a},{b}" for a, b in triangle)
        self.parts.append(f'<polygon points="{value}" fill="{color}"/>')

    @staticmethod
    def hits_box(a, b, box, padding=0):
        x, y, w, h = box
        x, y, w, h = x - padding, y - padding, w + padding * 2, h + padding * 2
        if a[0] == b[0]:
            return x < a[0] < x + w and max(min(a[1], b[1]), y) < min(max(a[1], b[1]), y + h)
        assert a[1] == b[1], (a, b)
        return y < a[1] < y + h and max(min(a[0], b[0]), x) < min(max(a[0], b[0]), x + w)

    def validate(self):
        for identity, source, target, points in self.edges:
            for name, node in self.nodes.items():
                if name in {source, target}:
                    continue
                assert not any(
                    self.hits_box(a, b, (node.x, node.y, node.w, node.h), 5)
                    for a, b in zip(points, points[1:], strict=False)
                ), (self.name, identity, "crosses node", name)
            for label_id, bounds in self.labels:
                assert not any(
                    self.hits_box(a, b, bounds, 3) for a, b in zip(points, points[1:], strict=False)
                ), (self.name, identity, "crosses label", label_id)
        segments = [
            (identity, a, b)
            for identity, _, _, points in self.edges
            for a, b in zip(points, points[1:], strict=False)
        ]
        for index, (identity, a, b) in enumerate(segments):
            for other, c, d in segments[index + 1 :]:
                if identity == other:
                    continue
                if a[0] == b[0] and c[1] == d[1]:
                    crossing = min(c[0], d[0]) < a[0] < max(c[0], d[0]) and min(a[1], b[1]) < c[
                        1
                    ] < max(a[1], b[1])
                elif a[1] == b[1] and c[0] == d[0]:
                    crossing = min(a[0], b[0]) < c[0] < max(a[0], b[0]) and min(c[1], d[1]) < a[
                        1
                    ] < max(c[1], d[1])
                elif a[0] == b[0] == c[0] == d[0]:
                    crossing = max(min(a[1], b[1]), min(c[1], d[1])) < min(
                        max(a[1], b[1]), max(c[1], d[1])
                    )
                elif a[1] == b[1] == c[1] == d[1]:
                    crossing = max(min(a[0], b[0]), min(c[0], d[0])) < min(
                        max(a[0], b[0]), max(c[0], d[0])
                    )
                else:
                    crossing = False
                assert not crossing, (self.name, "crossing/overlap", identity, other)
        for identity, box in self.labels:
            for node in self.nodes.values():
                x, y, w, h = box
                assert not (
                    max(x, node.x) < min(x + w, node.x + node.w)
                    and max(y, node.y) < min(y + h, node.y + node.h)
                ), (self.name, identity, "label overlaps node", node.name)
        for index, (identity, a) in enumerate(self.labels):
            for other, b in self.labels[index + 1 :]:
                assert not (
                    max(a[0], b[0]) < min(a[0] + a[2], b[0] + b[2])
                    and max(a[1], b[1]) < min(a[1] + a[3], b[1] + b[3])
                ), (self.name, "overlapping labels", identity, other)

    def save(self, output, *, validate=True):
        if validate:
            self.validate()
        path = output / f"{self.name}.svg"
        path.write_text("\n".join(self.parts) + "\n</svg>\n")
        return {
            "file": path.name,
            "nodes": len(self.nodes),
            "connectors": len(self.edges),
            "geometryChecked": validate,
            "sequenceRows": getattr(self, "sequence_rows", 0),
        }


def overall():
    f = Figure(
        "architecture-v1.2",
        "전체 시스템 구성과 제어 흐름",
        (
            "API 화살표는 요청 방향이다. 클립 복제·직접 업로드는 클립 저장 "
            "경로도, 각 요청·응답은 상세 흐름도를 따른다."
        ),
    )
    f.panel(900, 130, 1470, 1550, "AWS 관리 서비스와 백엔드")
    f.panel(30, 470, 700, 570, "산업 현장  |  두 설치는 같은 코드와 별도 상태 사용")
    f.panel(30, 1150, 700, 510, "사무실  |  수신기와 워커는 PC 또는 지원 NAS에서 실행")
    f.node(
        "customer",
        60,
        170,
        640,
        180,
        "고객 클라이언트",
        ["직원 웹·모바일 / 허용된 파트너 서버", "이벤트·시계열·AAS 조회 / 클립 요청·상태·다운로드"],
    )
    f.node(
        "cognito",
        930,
        200,
        380,
        150,
        "Cognito User Pool",
        ["직원 로그인 / 설치별 M2M 토큰", "회사 IdP 연동은 별도 설정"],
        color=PURPLE,
        size=23,
    )
    f.node(
        "cctv-ai",
        70,
        550,
        240,
        150,
        "CCTV AI·센서 수집",
        ["감지·온도 이벤트 JSON", "녹화 클립·매핑"],
        size=23,
    )
    f.node(
        "cctv",
        420,
        550,
        270,
        150,
        "CCTV field 엣지",
        ["SQLite outbox·manifest", "관리 사본·예비 업로드"],
        size=21,
    )
    f.node(
        "robot-ai",
        70,
        820,
        240,
        150,
        "로봇 AI·녹화",
        ["감지 이벤트 JSON", "완성 클립·매핑"],
        size=23,
    )
    f.node(
        "robot",
        420,
        820,
        270,
        150,
        "로봇 field 엣지",
        ["SQLite outbox·manifest", "관리 사본·예비 업로드"],
        size=21,
    )
    f.node(
        "gateway",
        930,
        550,
        380,
        480,
        "API Gateway REST API",
        [
            "WAF는 API stage에 연결",
            "Cognito Authorizer",
            "HTTPS JSON 요청과 응답",
            "토큰·scope 1차 검사",
            "요청 제한 / 접근 로그",
            "기능별 권한은 백엔드 재검사",
        ],
        size=24,
    )
    f.node(
        "api",
        1440,
        490,
        480,
        640,
        "ApiFunction  |  한 개 Lambda",
        [
            "FastAPI 라우팅 / 공통 인증·데이터 권한",
            "이벤트 수신 · 이벤트·TimeSeries 조회",
            "AAS 조회 어댑터",
            "Discovery · Registry · Shell",
            "Nameplate · TechnicalData · TimeSeries",
            "Submodel · submodel-refs · $value",
            "클립 요청 · 상태 · GET/PUT 서명 URL",
            "작업 claim · lease · 업로드 완료 검증",
            "사본 등록 · 전송 권한 · 정리 보호",
        ],
        size=23,
    )
    f.node(
        "db",
        2020,
        610,
        320,
        380,
        "RDS PostgreSQL 17",
        [
            "자산·센터·등록 주체·권한",
            "이벤트·자산 연결·영상 상태",
            "file_requests / job_leases",
            "clip_replicas",
            "archive_transfers",
            "AAS도 같은 DB 조회",
        ],
        size=22,
    )
    f.node(
        "sqs",
        2020,
        1140,
        320,
        200,
        "SQS 설치별 큐 + DLQ",
        ["CCTV / 로봇 / 사무실", "기존 legacy 큐 유지", "SQS 접근은 백엔드만 수행"],
        size=22,
    )
    f.node(
        "s3",
        2020,
        170,
        320,
        180,
        "비공개 S3 클립 버킷",
        ["요청한 클립만 임시 보관", "객체·시도별 PUT / GET URL", "영상 바이트는 Lambda 미경유"],
        size=21,
    )
    f.node(
        "receiver",
        70,
        1250,
        280,
        160,
        "HTTPS 수신기",
        ["TLS 프록시 + archive receiver", "크기·SHA-256·볼륨 확인", "NAS 확정 후 완료 보고"],
        size=20,
    )
    f.node(
        "worker",
        420,
        1250,
        270,
        160,
        "사무실 archive 워커",
        ["작업 API claim / lease", "NAS 읽기 → S3 PUT", "상태·업로드 결과 보고"],
        size=21,
    )
    f.node(
        "nas",
        70,
        1490,
        280,
        130,
        "사무실 NAS",
        ["검증 클립 / .dsc-volume-id", "로컬·마운트 경로로 접근"],
        color=ORANGE,
        size=20,
    )
    f.node(
        "timer",
        930,
        1450,
        380,
        170,
        "EventBridge 예약 규칙",
        ["rate(1 minute)", "SAM Schedule로 유지관리 호출"],
        color=GRAY,
        size=23,
    )
    f.node(
        "maintenance",
        1440,
        1450,
        480,
        170,
        "MaintenanceFunction  |  별도 Lambda",
        [
            "미수락·lease 만료 / 재발행·재배정",
            "기한 만료 / 이전 S3 객체 안전 정리",
            "DB·SQS·S3·관측 연결: AWS 배포 상세",
        ],
        color=GRAY,
        size=22,
    )
    f.edge(
        "A01",
        "customer",
        "cognito",
        [(700, 260), (930, 260)],
        "로그인·토큰",
        (813, 237),
        color=PURPLE,
    )
    f.edge(
        "A02",
        "cognito",
        "gateway",
        [(1120, 350), (1120, 550)],
        "User Pool 연동 설정",
        (1260, 430),
        color=GRAY,
        dash=True,
    )
    f.edge(
        "A03",
        "customer",
        "gateway",
        [(620, 350), (620, 400), (850, 400), (850, 610), (930, 610)],
        "고객 조회·클립 요청",
        (725, 445),
    )
    f.edge("F01", "cctv-ai", "cctv", [(310, 625), (420, 625)], "입력", (365, 605))
    f.edge("F02", "robot-ai", "robot", [(310, 895), (420, 895)], "입력", (365, 875))
    f.edge(
        "F03",
        "cctv",
        "gateway",
        [(690, 625), (800, 625), (800, 700), (930, 700)],
        "이벤트·작업",
        (800, 735),
    )
    f.edge(
        "F04",
        "robot",
        "gateway",
        [(690, 895), (800, 895), (800, 840), (930, 840)],
        "이벤트·작업",
        (810, 810),
    )
    f.edge(
        "O01",
        "receiver",
        "gateway",
        [
            (350, 1300),
            (380, 1300),
            (380, 1240),
            (760, 1240),
            (760, 1100),
            (800, 1100),
            (800, 940),
            (930, 940),
        ],
        "수신 권한·완료",
        (580, 1220),
    )
    f.edge(
        "O02",
        "worker",
        "gateway",
        [(690, 1340), (850, 1340), (850, 1000), (930, 1000)],
        "claim·lease·결과",
        (1050, 1100),
    )
    f.edge(
        "O03", "receiver", "nas", [(210, 1410), (210, 1490)], "NAS 확정", (315, 1455), color=ORANGE
    )
    f.edge(
        "O04",
        "worker",
        "nas",
        [(550, 1410), (550, 1540), (350, 1540)],
        "NAS 읽기",
        (475, 1500),
        color=ORANGE,
    )
    f.edge("A04", "gateway", "api", [(1310, 800), (1440, 800)], "호출", (1375, 777))
    f.edge("A05", "api", "db", [(1920, 790), (2020, 790)], "SQL", (1970, 765))
    f.edge(
        "A06",
        "api",
        "sqs",
        [(1920, 1050), (1960, 1050), (1960, 1240), (2020, 1240)],
        "큐 송수신",
        (2110, 1090),
    )
    f.edge(
        "A07",
        "api",
        "s3",
        [(1920, 550), (1970, 550), (1970, 430), (2180, 430), (2180, 350)],
        "S3 HEAD 검증",
        (2170, 485),
    )
    f.edge(
        "A08",
        "customer",
        "s3",
        [(380, 170), (380, 120), (2180, 120), (2180, 170)],
        "서명 GET URL로 직접 다운로드",
        (1680, 151),
        color=ORANGE,
    )
    f.edge(
        "A09",
        "timer",
        "maintenance",
        [(1310, 1535), (1440, 1535)],
        "매 1분",
        (1375, 1507),
        color=GRAY,
    )
    f.text(60, 1008, "복제·주·예비 업로드: 클립 저장 경로와 상세 흐름도 참조", 21, anchor="start")
    f.text(
        45,
        1725,
        (
            "파랑: HTTPS API·DB·큐 요청   주황: 클립 전송·파일 접근   "
            "보라: 토큰 발급   회색 점선: 설정 의존 관계"
        ),
        23,
        anchor="start",
    )
    return f


def media():
    f = Figure(
        "media-paths-v1.2",
        "클립 저장 위치와 주 업로드 및 예비 경로",
        (
            "화살표는 요청 시작 방향이다. GET·파일 읽기의 클립 바이트는 "
            "응답으로 돌아온다. 제어 API와 복구 절차는 각 상세 흐름도에서 "
            "표시한다."
        ),
        height=1080,
    )
    f.panel(30, 210, 390, 640, "현장 PC의 관리 사본")
    f.panel(650, 210, 1000, 640, "사무실  |  NAS 포트는 인터넷 비공개")
    f.panel(1800, 210, 560, 640, "AWS / 고객")
    f.node(
        "cctv",
        65,
        300,
        320,
        170,
        "CCTV field 엣지",
        ["해당 CCTV 클립의 검증 사본", "기본 30일 / 미확인 건 보존", "자기 이벤트의 예비 업로드"],
        size=22,
    )
    f.node(
        "robot",
        65,
        650,
        320,
        170,
        "로봇 field 엣지",
        ["해당 로봇 클립의 검증 사본", "기본 30일 / 미확인 건 보존", "자기 이벤트의 예비 업로드"],
        size=22,
    )
    f.node(
        "receiver",
        710,
        310,
        270,
        170,
        "사무실 HTTPS 수신기",
        ["TLS → 127.0.0.1 수신기", "전송 권한 / 크기·SHA-256", "ACK 전 NAS·서버 확정"],
        size=21,
    )
    f.node(
        "nas",
        1100,
        310,
        340,
        170,
        "사무실 NAS 클립",
        ["이벤트 / revision / clipId.mp4", "fsync · 원자 확정 · manifest", "볼륨 식별 마커 확인"],
        size=22,
    )
    f.node(
        "worker",
        1100,
        650,
        340,
        170,
        "사무실 archive 업로더",
        ["주 담당 / 요청 클립만 읽기", "API lease와 서명 PUT URL", "현장과 동일한 업로드 코드"],
        size=22,
    )
    f.node(
        "s3",
        1870,
        570,
        420,
        190,
        "S3 요청 클립 객체",
        [
            "비공개 / 시도별 별도 키",
            "실제 크기·SHA-256 확인 후 READY",
            "URL 기본 15분 PUT / 최대 5분 GET",
        ],
        size=23,
    )
    f.node(
        "customer",
        1870,
        300,
        420,
        150,
        "고객 클라이언트",
        ["API에서 READY·다운로드 URL 조회", "브라우저·서버가 S3 직접 접근"],
        size=23,
    )
    f.edge(
        "D01", "cctv", "receiver", [(385, 385), (710, 385)], "HTTPS 복제", (545, 356), color=ORANGE
    )
    f.edge(
        "D02",
        "robot",
        "receiver",
        [(385, 735), (570, 735), (570, 435), (710, 435)],
        "HTTPS 복제",
        (540, 785),
        color=ORANGE,
    )
    f.edge("D03", "receiver", "nas", [(980, 395), (1100, 395)], "확정", (1040, 367), color=ORANGE)
    f.edge(
        "D04", "worker", "nas", [(1270, 650), (1270, 480)], "파일 읽기", (1400, 555), color=ORANGE
    )
    f.edge(
        "D05",
        "worker",
        "s3",
        [(1440, 735), (1870, 735)],
        "주 업로드 HTTPS PUT",
        (1650, 706),
        color=ORANGE,
    )
    f.edge(
        "D06",
        "cctv",
        "s3",
        [(65, 385), (15, 385), (15, 155), (1740, 155), (1740, 610), (1870, 610)],
        "사무실 장애 시 CCTV 예비 PUT",
        (1150, 142),
        color=RED,
        dash=True,
    )
    f.edge(
        "D07",
        "robot",
        "s3",
        [(225, 820), (225, 920), (2080, 920), (2080, 760)],
        "사무실 장애 시 로봇 예비 PUT",
        (1150, 955),
        color=RED,
        dash=True,
    )
    f.edge(
        "D08", "customer", "s3", [(2080, 450), (2080, 570)], "직접 GET", (2215, 520), color=ORANGE
    )
    f.text(
        45,
        1025,
        (
            "D06·D07은 NAS에 도달할 필요가 없다. 현장→AWS 회선과 검증 "
            "사본이 살아 있어야 하며, 다른 엣지의 클립을 임의로 대신 "
            "업로드하지 않는다."
        ),
        23,
        anchor="start",
    )
    return f


def wrap(text, width, size):
    font = ImageFont.truetype(str(FONT), size)
    lines = []
    current = ""
    for word in text.split(" "):
        value = f"{current} {word}".strip()
        if font.getlength(value) > width and current:
            lines.append(current)
            current = word
        else:
            current = value
    if current:
        lines.append(current)
    return lines


def sequence(name, title, subtitle, lanes, rows, *, width=2400, row_height=92, notes=()):
    height = 280 + row_height * len(rows) + 70 + 36 * len(notes)
    f = Figure(name, title, subtitle, width, height)
    f.sequence_rows = len(rows)
    f.sequence_lanes = lanes
    f.sequence_messages = rows
    spacing = (width - 500) / (len(lanes) - 1)
    centers = [250 + spacing * i for i in range(len(lanes))]
    header_w = min(400, spacing - 35)
    for center, (heading, subheading) in zip(centers, lanes, strict=True):
        f.parts.append(
            f'<rect x="{center - header_w / 2}" y="130" width="{header_w}" height="100" '
            'rx="5" fill="#F0F4F8" stroke="#A3ADB8" stroke-width="1.5"/>'
        )
        f.text(center, 171, heading, 25, bold=True)
        f.text(center, 207, subheading, 22)
        f.parts.append(
            f'<path d="M{center},230 V{height - 70 - 36 * len(notes)}" fill="none" '
            'stroke="#C0C7CF" stroke-width="1.5" stroke-dasharray="5 7"/>'
        )
    for index, (identity, source, target, label, kind) in enumerate(rows):
        y = 310 + index * row_height
        color = (
            ORANGE
            if kind == "media"
            else RED
            if kind == "reject"
            else GRAY
            if kind == "return"
            else BLUE
        )
        a, b = centers[source], centers[target]
        if source == target:
            direction = 1 if source < len(lanes) - 1 else -1
            outer = a + direction * min(130, spacing / 3)
            f.arrow([(a, y - 3), (outer, y - 3), (outer, y + 24), (a, y + 24)], color=color)
            lines = wrap(f"{identity}  {label}", spacing - 50, 23)
            text_x = a + direction * (spacing / 2 - 20)
        else:
            f.arrow([(a, y + 18), (b, y + 18)], color=color, dash=kind == "return")
            lines = wrap(f"{identity}  {label}", abs(a - b) - 50, 23)
            text_x = (a + b) / 2
        assert len(lines) * 27 <= row_height - 17, (name, identity, lines)
        for number, line in enumerate(lines):
            f.text(text_x, y - 10 - (len(lines) - 1 - number) * 27, line, 23, color=color)
    for index, note in enumerate(notes):
        f.text(
            45, height - 35 - 36 * (len(notes) - 1 - index), note, 23, anchor="start", color=GRAY
        )
    return f


def sequences():
    return [
        sequence(
            "customer-access-v1.2",
            "고객 인증과 이벤트 및 AAS 조회",
            (
                "직원·파트너·현장 설치의 인증 주체를 구분하고, API Gateway "
                "검사 뒤에도 백엔드가 기능·데이터 권한을 확인한다."
            ),
            [
                ("고객 클라이언트", "직원 웹·앱 / 파트너 서버"),
                ("Cognito / 회사 IdP", "직원 연동은 설정 후 사용"),
                ("API Gateway + ApiFunction", "보호된 HTTPS 요청"),
                ("PostgreSQL", "권한·자산·이벤트"),
            ],
            [
                ("Q01", 0, 1, "직원 Auth Code + PKCE 또는 파트너 client_credentials", "api"),
                ("Q02", 1, 0, "access token 반환; 직원은 회사 IdP 연동 로그인", "return"),
                ("Q03", 0, 2, "Bearer JWT로 이벤트·TimeSeries 또는 AAS 읽기 요청", "api"),
                ("Q04", 2, 2, "Gateway Authorizer + 백엔드 JWT·client·scope 검증", "api"),
                ("Q05", 2, 3, "활성 주체·센터·모든 연결 자산·공개 Submodel 범위 조회", "api"),
                ("Q06", 3, 2, "권한 필터를 적용할 등록 정보·이벤트·자산 데이터", "return"),
                ("Q07", 2, 2, "일반 조회 또는 같은 Lambda의 AAS 어댑터로 응답 조립", "api"),
                ("Q08", 2, 0, "이벤트 JSON 또는 AAS Shell·Submodel·Registry·Discovery", "return"),
            ],
            notes=[
                (
                    "AAS TimeSeries는 같은 DB의 조회 결과와 인증된 /timeseries "
                    "경로를 제공한다. 별도 AAS DB나 별도 AAS Lambda는 없다."
                ),
                (
                    "영상 요청·다운로드는 클립 요청 흐름도를 따른다. 파트너의 "
                    "이력·영상 접근은 별도 등록 권한을 요구한다."
                ),
            ],
        ),
        sequence(
            "replication-flow-v1.2",
            "이벤트 접수와 사무실 NAS 복제의 완료 조건",
            (
                "CCTV·로봇 각각 동일 절차를 수행한다. 로컬 파일 확정·DB "
                "저장·NAS 저장·서버 STORED 확인을 서로 다른 단계로 구분한다."
            ),
            [
                ("field 엣지", "AI inbox / 관리 사본 / SQLite"),
                ("AWS 제어 API", "ApiFunction + PostgreSQL"),
                ("사무실 수신기", "HTTPS / 로컬 manifest"),
                ("사무실 NAS", "서비스 전용 마운트·볼륨"),
            ],
            [
                ("R01", 0, 0, "완성 JSON만 접수; 클립 관리 사본·outbox 영속 확정", "api"),
                ("R02", 0, 1, "POST /ingest — 동일 eventId·최초 본문 유지", "api"),
                ("R03", 1, 0, "이벤트·자산·영상 트랜잭션 commit 후 201 / 중복 200", "return"),
                ("R04", 0, 1, "POST /edge-server/replicas — 현장 크기·SHA-256 등록", "api"),
                ("R05", 0, 1, "POST /edge-server/archive/prepare — 수신자·전송 권한", "api"),
                ("R06", 1, 0, "transferId·수신 URL·짧은 전송 capability 반환", "return"),
                ("R07", 0, 2, "기존 파일 verify; 404이면 PUT /archive/transfers/{id}", "media"),
                ("R08", 2, 1, "POST .../archive/{id}/authorize — 수신기 자체 JWT로 검증", "api"),
                ("R09", 2, 3, "크기·SHA-256·볼륨 검증; fsync·원자 확정", "media"),
                ("R10", 2, 1, "로컬 manifest 확정 후 POST .../archive/{id}/complete", "api"),
                ("R11", 1, 2, "정확한 클립·revision·수신자의 STORED receipt", "return"),
                ("R12", 2, 0, "서버 확인을 마친 receipt; field도 일치 검사 후 기록", "return"),
            ],
            notes=[
                (
                    "R07의 verify도 R08~R12의 재검증·보고를 거친다. ACK 유실·부분 "
                    "전송·재시작은 같은 식별자와 내용으로 조정한다."
                ),
                (
                    "클립 바이트는 field→사무실로 전송하며 AWS Lambda를 경유하지 "
                    "않는다. 원본 생성 프로그램의 보관 정책은 별도다."
                ),
            ],
        ),
        sequence(
            "clip-flow-v1.2",
            "고객의 클립 요청부터 업로드 검증과 직접 다운로드까지",
            (
                "공통 백엔드가 SQS를 다룬다. 업로더는 HTTPS 작업 API만 "
                "호출하고 S3에는 객체별 서명 URL로 접근한다."
            ),
            [
                ("고객", "직원 / 영상 허용 파트너"),
                ("AWS 제어 API", "ApiFunction / DB / SQS"),
                ("선택된 업로더", "사무실 우선 / 해당 현장 예비"),
                ("로컬 검증 사본", "사무실 NAS 또는 field cache"),
                ("비공개 S3", "시도별 클립 객체"),
            ],
            [
                ("C01", 0, 1, "POST /files/{eventId}/requests", "api"),
                ("C02", 1, 1, "권한·media 검사; 작업 영속 저장 후 설치별 큐 발행", "api"),
                ("C03", 1, 0, "신규 202 + requestId·상태 URL; READY 재사용 시 200", "return"),
                ("C04", 2, 1, "POST /edge-server/jobs/claim", "api"),
                ("C05", 1, 1, "자기 설치 큐 Receive; DB 잠금·배정·단일 lease 검사", "api"),
                ("C06", 1, 2, "requestId·eventId·clipId·revision·leaseToken·attempt", "return"),
                ("C07", 2, 3, "manifest·경로·크기·SHA-256을 확인하며 클립 읽기", "media"),
                ("C08", 2, 1, "POST .../jobs/{id}/upload-url; 필요 시 /lease 연장", "api"),
                ("C09", 1, 2, "이번 시도의 객체 키와 제한된 PUT URL·필수 헤더", "return"),
                ("C10", 2, 4, "서명 URL로 HTTPS PUT; 영상은 Lambda 미경유", "media"),
                ("C11", 2, 1, "POST .../jobs/{id}/complete — 현재 leaseToken", "api"),
                ("C12", 1, 4, "HEAD ChecksumMode=ENABLED; 실제 크기·SHA-256 대조", "api"),
                ("C13", 1, 1, "유효 lease·revision·요청자 권한을 재확인하고 READY", "api"),
                ("C14", 0, 1, "GET /file-requests/{id} 또는 GET /files/{eventId}", "api"),
                ("C15", 1, 0, "READY 200 + 짧은 GET URL; 처리 중은 202", "return"),
                ("C16", 0, 4, "서명 URL로 HTTPS GET — 클립 직접 다운로드", "media"),
            ],
            width=2700,
            row_height=98,
            notes=[
                (
                    "실패는 /jobs/{id}/fail로 보고한다. ETag는 SHA-256으로 "
                    "사용하지 않는다. AAS Endpoint에는 NAS 경로나 서명 URL을 "
                    "저장하지 않는다."
                )
            ],
        ),
        sequence(
            "failover-flow-v1.2",
            "사무실 장애와 오래된 시도의 차단",
            (
                "예비 업로더는 같은 이벤트의 검증 현장 사본을 가진 발신 "
                "엣지다. 사무실과 독립된 현장→AWS 회선이 필요하다."
            ),
            [
                ("AWS 제어·유지관리", "DB 상태 / 설치별 SQS"),
                ("사무실 업로더", "주 담당 / 중단 또는 실패"),
                ("해당 field 엣지", "CCTV 또는 로봇의 자기 사본"),
                ("S3", "시도별 분리된 객체"),
            ],
            [
                ("H01", 1, 0, "heartbeat NAS 비정상 또는 /fail SOURCE_UNREACHABLE", "api"),
                ("H02", 0, 0, "또는 heartbeat·미수락·lease 기한 경과를 점검", "api"),
                ("H03", 0, 0, "유효 lease는 보호; 만료·실패 시 재시도·배정 갱신", "api"),
                ("H04", 0, 0, "동일 event·clip·revision·크기·해시의 건강한 현장 선택", "api"),
                ("H05", 2, 0, "작업 claim — 새 dispatchVersion과 단일 lease 확인", "api"),
                ("H06", 0, 2, "새 attempt·leaseToken·이번 시도의 서명 PUT URL", "return"),
                ("H07", 2, 3, "사무실 NAS를 거치지 않고 현장 사본 PUT", "media"),
                ("H08", 1, 0, "이전 attempt의 complete / fail / lease 통지", "api"),
                ("H09", 0, 1, "404 또는 409; 현재 작업·사본·완료 상태 변경 금지", "reject"),
                ("H10", 2, 0, "정상 complete → S3 실물 검증 → READY", "api"),
            ],
            notes=[
                (
                    "기본값: heartbeat 유효 180초, 미수락 재배정 60초, lease "
                    "300초·60초 갱신, 유지관리 1분 주기. 즉시 전환을 보장하지 "
                    "않는다."
                ),
                (
                    "양쪽 사본·회선이 모두 불가하면 기한·재시도 정책에 따라 대기 "
                    "또는 실패한다. 30일 밖 사본은 NAS·백업 가용성에 따른다."
                ),
            ],
        ),
        sequence(
            "retention-flow-v1.2",
            "30일 현장 사본 정리와 중단 후 복구",
            (
                "기간 만료만으로 지우지 않는다. NAS 재검증·작업 보호·서버의 "
                "신규 배정 차단을 확인한 관리 사본만 제거한다."
            ),
            [
                ("field 엣지", "SQLite / cache / 파일 잠금"),
                ("사무실 수신기", "정확한 전송·볼륨 확인"),
                ("사무실 NAS", "완성된 같은 내용의 파일"),
                ("AWS 제어 API", "사본·전송·작업 DB 잠금"),
            ],
            [
                ("T01", 0, 0, "endedAt 기준 기본 30일 도래; 미전송 outbox는 별도 보존", "api"),
                ("T02", 0, 1, "유효 transfer로 POST /archive/transfers/{id}/verify", "api"),
                ("T03", 1, 2, "NAS 실제 바이트 재읽기; 크기·SHA-256·볼륨 확인", "media"),
                ("T04", 1, 3, "전송 authorize / complete로 같은 사본 STORED 확인", "api"),
                ("T05", 1, 0, "동일 metadata의 STORED receipt 반환", "return"),
                ("T06", 0, 3, "POST /edge-server/replicas/retire", "api"),
                ("T07", 3, 3, "건강한 NAS·STORED 사본·진행 중 작업·경쟁 검사", "api"),
                ("T08", 3, 0, "RETIRED 반환 — 이 현장 사본의 신규 배정 차단", "return"),
                ("T09", 0, 0, "로컬 RETIRING 기록 → 해시 확인·삭제 → DELETED", "api"),
            ],
            notes=[
                (
                    "전송·NAS·권한·작업 조건 불충족 시 보존한다. retire ACK "
                    "유실·정리 중 종료는 RETIRING 상태와 서버 응답으로 조정한다."
                ),
                (
                    "생성 프로그램 원본은 이 정리 대상이 아니다. 원본+관리 사본의 "
                    "합산 용량과 장기 장애 누적량, 별도 NAS 백업을 산정한다."
                ),
            ],
        ),
    ]


def operations():
    f = Figure(
        "aws-operations-v1.2",
        "AWS 배포 경계와 유지관리 및 운영 관측",
        (
            "네트워크·예약 처리·관측의 세 관계를 나누어 표시한다. 반복 "
            "표시는 같은 리소스이며 별도 복제 서버를 뜻하지 않는다."
        ),
        height=1640,
    )
    f.panel(
        35,
        135,
        2330,
        490,
        "네트워크 경로  |  Lambda·DB private / NAT public / 나머지는 AWS 관리 서비스",
    )
    f.node(
        "functions",
        100,
        290,
        430,
        170,
        "API / Maintenance Lambda",
        [
            "private subnet A/B에 VPC 연결",
            "서로 다른 실행 역할 / SG 공유",
            "Lambda 서비스 호출로 실행",
        ],
        size=23,
    )
    f.node(
        "rds",
        760,
        260,
        430,
        150,
        "RDS PostgreSQL",
        ["private · 암호화 · TLS 5432", "Lambda SG만 허용 / 백업 14일"],
        size=23,
    )
    f.node(
        "nat",
        760,
        465,
        430,
        140,
        "개발 NAT Gateway",
        ["public subnet / 단일 AZ", "두 private subnet의 outbound"],
        size=23,
    )
    f.node(
        "endpoint",
        1400,
        260,
        350,
        150,
        "S3 Gateway Endpoint",
        ["private route table 연결", "두 함수의 S3 경로"],
        size=23,
    )
    f.node(
        "s3",
        1950,
        260,
        350,
        150,
        "S3 ClipBucket",
        ["HEAD 실제 크기·SHA-256", "이전 객체 DELETE"],
        size=23,
    )
    f.node(
        "services",
        1400,
        465,
        900,
        140,
        "AWS HTTPS Endpoint",
        [
            "SQS 설치별 큐·DLQ / Secrets Manager / Cognito JWKS",
            "SQS 송수신·재발행 / DB·cursor 비밀 / 검증용 공개키",
        ],
        size=23,
    )
    f.edge("E02", "functions", "rds", [(530, 350), (760, 350)], "SQL/TLS 5432", (645, 323))
    f.edge(
        "E04",
        "functions",
        "nat",
        [(530, 415), (650, 415), (650, 535), (760, 535)],
        "egress",
        (655, 570),
        color=GRAY,
    )
    f.edge(
        "E06a",
        "functions",
        "endpoint",
        [(315, 290), (315, 220), (1575, 220), (1575, 260)],
        "S3 서비스 호출",
        (1000, 207),
        color=ORANGE,
    )
    f.edge(
        "E06b", "endpoint", "s3", [(1750, 335), (1950, 335)], "S3 HTTPS", (1850, 308), color=ORANGE
    )
    f.edge(
        "E05", "nat", "services", [(1190, 535), (1400, 535)], "HTTPS 443", (1295, 508), color=GRAY
    )
    f.panel(35, 675, 2330, 410, "유지관리 논리 호출  |  동일 DB·큐·버킷을 재표시")
    f.node(
        "timer",
        100,
        775,
        400,
        150,
        "EventBridge 예약 규칙",
        ["rate(1 minute)", "SAM Schedule"],
        size=23,
    )
    f.node(
        "maintenance",
        710,
        775,
        430,
        150,
        "MaintenanceFunction",
        ["lease·기한·미수락 점검", "재발행·재배정 / 정리"],
        size=23,
    )
    f.node(
        "state",
        1410,
        775,
        400,
        150,
        "PostgreSQL / 설치별 SQS",
        ["DB 상태·배정 갱신", "전송 실패 작업 재발행"],
        size=23,
    )
    f.node(
        "cleanup",
        1960,
        775,
        340,
        150,
        "S3 이전 시도 객체",
        ["현재 작업·URL 기한 보호", "삭제 가능 시점 후 DELETE"],
        size=22,
    )
    f.edge(
        "E01",
        "timer",
        "maintenance",
        [(500, 850), (710, 850)],
        "매 1분 호출",
        (605, 823),
        color=GRAY,
    )
    f.edge("E03", "maintenance", "state", [(1140, 850), (1410, 850)], "상태·큐 처리", (1275, 823))
    f.edge(
        "E08",
        "maintenance",
        "cleanup",
        [(925, 925), (925, 1010), (2130, 1010), (2130, 925)],
        "안전 조건 후 객체 정리",
        (1530, 1048),
        color=ORANGE,
    )
    f.panel(
        35,
        1135,
        2330,
        430,
        "관측과 감사 경로  |  현장·사무실의 로컬 로그는 journal 또는 컨테이너 로그에 남음",
    )
    f.node(
        "sources",
        100,
        1240,
        420,
        190,
        "API Gateway / 두 Lambda",
        [
            "API 접근 로그 / 구조화 실행 로그",
            "함수 추적 / heartbeat 통계",
            "비밀·본문·서명 URL 미기록",
        ],
        size=22,
    )
    f.node(
        "logs",
        730,
        1240,
        420,
        190,
        "CloudWatch / X-Ray",
        ["API·함수 로그와 메트릭", "분산 요청 추적", "이벤트·작업 추적 ID 구분"],
        size=23,
    )
    f.node(
        "alarms",
        1370,
        1240,
        380,
        190,
        "CloudWatch 경보",
        ["API 5xx / 유지관리 오류", "사본 격리 / 디스크 부족", "메시지 자동 발송은 현 범위 밖"],
        size=21,
    )
    f.node(
        "audit",
        1870,
        1240,
        430,
        250,
        "S3 접근 감사 경로",
        [
            "ClipBucket → AuditBucket",
            "S3 서버 접근 로그",
            "CloudTrail 데이터 이벤트는",
            "ClipAuditMode에 따라 구성",
            "synthetic 개발은 합성 클립만",
        ],
        size=22,
    )
    f.edge(
        "E07a", "sources", "logs", [(520, 1335), (730, 1335)], "로그·추적", (625, 1308), color=GRAY
    )
    f.edge(
        "E07b",
        "logs",
        "alarms",
        [(1150, 1335), (1370, 1335)],
        "지표·필터",
        (1260, 1308),
        color=GRAY,
    )
    f.text(
        45,
        1605,
        (
            "E09는 각 실행 역할·설치별 인증·버킷/큐 정책의 권한 관계다. "
            "네트워크 선으로 표현하지 않으며 문서의 역할별 권한표를 따른다."
        ),
        23,
        anchor="start",
    )
    return f


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/diagrams"))
    args = parser.parse_args()
    FONT = args.font
    args.output.mkdir(parents=True, exist_ok=True)
    steps = sequences()
    clip = steps[2]
    clip_request = sequence(
        "clip-request-v1.2",
        "고객 클립 요청과 작업 배정 및 업로드 URL 발급",
        (
            "고객 요청을 DB에 보존하고 설치별 큐와 단일 lease로 처리자를 "
            "정한다. 다음 도면은 실제 PUT·완료 검증·고객 다운로드를 잇는다."
        ),
        clip.sequence_lanes,
        clip.sequence_messages[:9],
        width=2700,
        row_height=98,
        notes=[
            (
                "C02의 큐 발행 실패는 영속 DB 표식으로 재발행한다. C04는 "
                "업로더의 HTTPS 폴링이며 SQS 직접 접속이 아니다."
            )
        ],
    )
    clip_complete = sequence(
        "clip-completion-v1.2",
        "S3 업로드 완료 검증과 고객 직접 다운로드",
        (
            "앞 도면 C01~C09에서 발급한 현재 시도의 PUT URL을 사용한다. "
            "PUT 성공과 READY 판정 및 고객의 GET을 구분한다."
        ),
        clip.sequence_lanes,
        clip.sequence_messages[9:],
        width=2700,
        row_height=98,
        notes=[
            (
                "실패는 /edge-server/jobs/{id}/fail, 연장은 /lease로 보고한다. "
                "실제 HEAD를 검증하기 전에는 READY가 아니다."
            )
        ],
    )
    figures = [overall(), media(), *steps, operations(), clip_request, clip_complete]
    results = [figure.save(args.output, validate=bool(figure.edges)) for figure in figures]
    print(json.dumps(results, ensure_ascii=False, indent=2))
