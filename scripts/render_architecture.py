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
    def __init__(self, name, title, subtitle="", width=1000, height=1240):
        self.name, self.width, self.height = name, width, height
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" role="img">',
            f"<title>{escape(title)}</title><desc>{escape(subtitle)}</desc>",
            '<rect width="100%" height="100%" fill="white"/>',
        ]
        self.nodes = {}
        self.node_line_counts = {}
        self.edges = []
        self.labels = []
        self.panels = []
        self.text(24, 39, title, 28, bold=True, anchor="start")
        if subtitle:
            self.text(24, 73, subtitle, 20, anchor="start", color=GRAY)

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
        self.panels.append([(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)])
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" '
            f'fill="{fill}" stroke="#A3ADB8" stroke-width="2" stroke-dasharray="10 7"/>'
        )
        bounds = self.text(x + 16, y + 29, title, 22, bold=True, anchor="start")
        self.labels.append(("boundary:" + title, bounds))

    def node(self, name, x, y, w, h, title, lines=(), *, color=BLUE, size=22):
        self.nodes[name] = Box(name, x, y, w, h)
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" '
            f'fill="white" stroke="{color}" stroke-width="2.4"/>'
        )
        headings = wrap(title, w - 24, size)
        body = [part for line in lines for part in wrap(line, w - 24, size)]
        rows = headings + body
        assert 1 <= len(rows) <= 2, (self.name, name, "node exceeds two lines", rows)
        self.node_line_counts[name] = len(rows)
        for index, line in enumerate(rows):
            baseline = y + h / 2 + size * 0.35 + (index - (len(rows) - 1) / 2) * (size + 8)
            box = self.text(x + w / 2, baseline, line, size, bold=index < len(headings))
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
        bounds = self.text(*label_at, f"{identity} {label}".strip(), 20, color=color)
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
            for boundary in self.panels:
                assert not any(
                    self.hits_box(a, b, box, 2)
                    for a, b in zip(boundary, boundary[1:], strict=False)
                ), (self.name, identity, "boundary crosses label")
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
            "maxNodeLines": max(self.node_line_counts.values(), default=0),
        }


def overall():
    f = Figure("architecture-v1.2", "전체 시스템 구성", height=1240)
    f.panel(20, 390, 430, 310, "산업 현장")
    f.panel(540, 390, 430, 310, "사무실")
    f.node("customer", 40, 100, 280, 72, "고객 클라이언트", ["직원·허용 파트너"])
    f.node("cognito", 680, 100, 280, 72, "Cognito", ["사용자·M2M 토큰"], color=PURPLE)
    f.node("gateway", 360, 245, 280, 72, "API Gateway", ["WAF·Authorizer"])
    f.node("cctv-ai", 40, 450, 160, 72, "CCTV AI·센서", ["이벤트·클립"], size=20)
    f.node("cctv", 260, 450, 180, 72, "CCTV 엣지", ["field"])
    f.node("robot-ai", 40, 580, 160, 72, "로봇 AI", ["이벤트·클립"], size=20)
    f.node("robot", 260, 580, 180, 72, "로봇 엣지", ["field"])
    f.node("receiver", 560, 450, 170, 72, "HTTPS 수신기", ["receiver"], size=20)
    f.node("worker", 790, 450, 170, 72, "업로드 워커", ["archive"])
    f.node("nas", 675, 610, 170, 72, "사무실 NAS", ["검증 클립"], color=ORANGE)
    f.node("api", 360, 770, 280, 72, "ApiFunction", ["백엔드 API"])
    f.node("db", 80, 950, 240, 72, "PostgreSQL", ["RDS"])
    f.node("sqs", 380, 950, 240, 72, "설치별 SQS", ["큐·DLQ"])
    f.node("s3", 680, 950, 240, 72, "비공개 S3", ["임시 클립"], color=ORANGE)
    f.node("timer", 80, 1120, 300, 72, "EventBridge", ["1분 주기"])
    f.node("maintenance", 620, 1120, 300, 72, "MaintenanceFunction", ["복구·만료·정리"], size=21)
    f.edge(
        "A01",
        "customer",
        "cognito",
        [(320, 136), (680, 136)],
        "로그인·토큰",
        (500, 119),
        color=PURPLE,
    )
    f.edge(
        "A02",
        "cognito",
        "gateway",
        [(820, 172), (820, 215), (570, 215), (570, 245)],
        "설정",
        (750, 200),
        color=GRAY,
        dash=True,
    )
    f.edge(
        "A03",
        "customer",
        "gateway",
        [(180, 172), (180, 215), (430, 215), (430, 245)],
        "고객 API",
        (250, 200),
    )
    f.edge("F01", "cctv-ai", "cctv", [(200, 486), (260, 486)], "", (230, 471))
    f.edge("F02", "robot-ai", "robot", [(200, 616), (260, 616)], "", (230, 601))
    f.edge(
        "F03", "cctv", "gateway", [(350, 450), (350, 360), (395, 360), (395, 317)], "", (320, 350)
    )
    f.edge("F04", "robot", "gateway", [(440, 616), (470, 616), (470, 317)], "", (420, 565))
    f.edge(
        "O01",
        "receiver",
        "gateway",
        [(645, 450), (645, 360), (605, 360), (605, 317)],
        "",
        (695, 380),
    )
    f.edge(
        "O02", "worker", "gateway", [(875, 450), (875, 335), (625, 335), (625, 317)], "", (820, 320)
    )
    f.edge(
        "O03",
        "receiver",
        "nas",
        [(645, 522), (645, 570), (710, 570), (710, 610)],
        "저장",
        (620, 600),
        color=ORANGE,
    )
    f.edge(
        "O04",
        "worker",
        "nas",
        [(875, 522), (875, 570), (810, 570), (810, 610)],
        "읽기",
        (910, 600),
        color=ORANGE,
    )
    f.edge("A04", "gateway", "api", [(500, 317), (500, 770)], "호출", (575, 735))
    f.edge("A05", "api", "db", [(360, 806), (200, 806), (200, 950)], "SQL", (270, 895))
    f.edge("A06", "api", "sqs", [(500, 842), (500, 950)], "큐", (565, 895))
    f.edge("A07", "api", "s3", [(640, 806), (800, 806), (800, 950)], "HEAD", (880, 895))
    f.edge(
        "A08",
        "customer",
        "s3",
        [(180, 100), (180, 75), (985, 75), (985, 986), (920, 986)],
        "직접 GET",
        (810, 62),
        color=ORANGE,
    )
    f.edge("A09", "timer", "maintenance", [(380, 1156), (620, 1156)], "예약 호출", (500, 1140))
    f.text(
        24,
        1225,
        "제어: 파랑  |  파일: 주황  |  인증: 보라  |  설정: 회색 점선",
        20,
        anchor="start",
        color=GRAY,
    )
    return f


def backend():
    f = Figure("backend-components-v1.2", "백엔드 기능과 공통 권한", height=1020)
    f.panel(20, 210, 960, 560, "ApiFunction 내부")
    f.node("gateway", 350, 90, 300, 72, "API Gateway", ["보호된 HTTPS API"])
    f.node("auth", 350, 265, 300, 72, "FastAPI 라우터", ["경로·기능 선택"])
    f.node("router", 350, 410, 300, 72, "인증·데이터 권한", ["주체·센터·자산"])
    f.node("ingest", 50, 600, 200, 72, "이벤트 수신", ["Ingest API"])
    f.node("query", 285, 600, 200, 72, "이벤트·시계열", ["조회 API"])
    f.node("aas", 515, 600, 200, 72, "AAS 조회", ["AasAdapter"])
    f.node("jobs", 750, 600, 200, 72, "클립·사본", ["작업·전송 제어"])
    f.node("db", 350, 880, 300, 72, "PostgreSQL", ["업무 상태·권한"])
    f.edge("B01", "gateway", "auth", [(500, 162), (500, 265)], "호출", (580, 198))
    f.edge("B02", "auth", "router", [(500, 337), (500, 410)], "권한 검사", (580, 385))
    f.edge(
        "B03", "router", "ingest", [(380, 482), (380, 525), (150, 525), (150, 600)], "", (150, 510)
    )
    f.edge(
        "B04", "router", "query", [(460, 482), (460, 550), (385, 550), (385, 600)], "", (425, 585)
    )
    f.edge("B05", "router", "aas", [(540, 482), (540, 550), (615, 550), (615, 600)], "", (660, 585))
    f.edge(
        "B06", "router", "jobs", [(620, 482), (620, 525), (850, 525), (850, 600)], "", (850, 510)
    )
    f.edge(
        "B07", "ingest", "db", [(150, 672), (150, 790), (390, 790), (390, 880)], "저장", (230, 750)
    )
    f.edge(
        "B08", "query", "db", [(385, 672), (385, 730), (445, 730), (445, 880)], "조회", (325, 715)
    )
    f.edge("B09", "aas", "db", [(615, 672), (615, 730), (555, 730), (555, 880)], "조회", (685, 715))
    f.edge(
        "B10", "jobs", "db", [(850, 672), (850, 790), (610, 790), (610, 880)], "상태", (790, 750)
    )
    f.text(
        24,
        1000,
        "네 기능은 한 Lambda 안의 모듈이며 동일 DB·권한 정책을 사용한다.",
        20,
        anchor="start",
        color=GRAY,
    )
    return f


def aas():
    f = Figure("aas-components-v1.2", "AAS 조회 기능과 데이터 연결", height=1180)
    f.panel(20, 360, 960, 630, "ApiFunction 내부의 AAS 처리")
    f.node("customer", 340, 90, 320, 72, "AAS 고객", ["직원·허용 파트너"])
    f.node("gateway", 340, 240, 320, 72, "보호 API", ["Gateway·권한 검사"])
    f.node("adapter", 340, 420, 320, 72, "AAS 어댑터", ["AasAdapter"])
    f.node("discovery", 50, 650, 250, 72, "Discovery", ["Shell 식별자 검색"])
    f.node("registry", 375, 650, 250, 72, "Registry", ["Descriptor 조회"])
    f.node("repository", 700, 650, 250, 72, "Repository", ["Shell 조회"])
    f.node("lookup", 50, 850, 250, 72, "자산 ID 조회", ["허용 자산 검색"])
    f.node("descriptor", 375, 850, 250, 72, "Endpoint 조립", ["Shell·Submodel"])
    f.node("submodel", 700, 850, 250, 72, "Submodel 조립", ["명판·기술·시계열"])
    f.node("db", 340, 1070, 320, 72, "PostgreSQL", ["등록 자산·이벤트"])
    f.edge("U01", "customer", "gateway", [(500, 162), (500, 240)], "조회", (580, 210))
    f.edge("U02", "gateway", "adapter", [(500, 312), (500, 420)], "라우팅", (600, 345))
    f.edge(
        "U03",
        "adapter",
        "discovery",
        [(400, 492), (400, 560), (175, 560), (175, 650)],
        "",
        (175, 545),
    )
    f.edge("U04", "adapter", "registry", [(500, 492), (500, 650)], "", (550, 610))
    f.edge(
        "U05",
        "adapter",
        "repository",
        [(600, 492), (600, 560), (825, 560), (825, 650)],
        "",
        (825, 545),
    )
    f.edge("U06", "discovery", "lookup", [(175, 722), (175, 850)], "", (220, 790))
    f.edge("U07", "registry", "descriptor", [(500, 722), (500, 850)], "", (550, 790))
    f.edge("U08", "repository", "submodel", [(825, 722), (825, 850)], "", (875, 790))
    f.edge(
        "U09", "lookup", "db", [(175, 922), (175, 1010), (400, 1010), (400, 1070)], "", (240, 1045)
    )
    f.edge("U10", "descriptor", "db", [(500, 922), (500, 1070)], "", (550, 1035))
    f.edge(
        "U11",
        "submodel",
        "db",
        [(825, 922), (825, 1010), (600, 1010), (600, 1070)],
        "",
        (765, 1045),
    )
    return f


def media():
    f = Figure("media-paths-v1.2", "클립 복제와 주·예비 업로드", height=1230)
    f.panel(20, 90, 960, 190, "현장 PC")
    f.panel(20, 360, 960, 390, "사무실")
    f.panel(300, 830, 400, 375, "클라우드·고객")
    f.node("cctv", 80, 155, 300, 72, "CCTV 엣지", ["로컬 검증 사본"])
    f.node("robot", 620, 155, 300, 72, "로봇 엣지", ["로컬 검증 사본"])
    f.node("receiver", 350, 420, 300, 72, "HTTPS 수신기", ["전송 검증"])
    f.node("nas", 75, 620, 300, 72, "사무실 NAS", ["확정 클립"], color=ORANGE)
    f.node("worker", 625, 620, 300, 72, "사무실 업로더", ["archive 워커"])
    f.node("s3", 350, 950, 300, 72, "비공개 S3", ["요청 클립 객체"], color=ORANGE)
    f.node("customer", 350, 1110, 300, 72, "고객 클라이언트", ["직접 다운로드"])
    f.edge(
        "D01",
        "cctv",
        "receiver",
        [(230, 227), (230, 320), (420, 320), (420, 420)],
        "HTTPS",
        (315, 305),
        color=ORANGE,
    )
    f.edge(
        "D02",
        "robot",
        "receiver",
        [(770, 227), (770, 320), (580, 320), (580, 420)],
        "HTTPS",
        (685, 305),
        color=ORANGE,
    )
    f.edge(
        "D03",
        "receiver",
        "nas",
        [(420, 492), (420, 555), (225, 555), (225, 620)],
        "저장",
        (320, 540),
        color=ORANGE,
    )
    f.edge("D04", "worker", "nas", [(625, 656), (375, 656)], "읽기", (500, 640), color=ORANGE)
    f.edge(
        "D05",
        "worker",
        "s3",
        [(775, 692), (775, 865), (620, 865), (620, 950)],
        "주 PUT",
        (855, 815),
        color=ORANGE,
    )
    f.edge(
        "D06",
        "cctv",
        "s3",
        [(80, 191), (10, 191), (10, 986), (350, 986)],
        "예비 PUT",
        (165, 970),
        color=RED,
        dash=True,
    )
    f.edge(
        "D07",
        "robot",
        "s3",
        [(920, 191), (990, 191), (990, 986), (650, 986)],
        "예비 PUT",
        (840, 970),
        color=RED,
        dash=True,
    )
    f.edge("D08", "customer", "s3", [(500, 1110), (500, 1022)], "GET", (590, 1075), color=ORANGE)
    return f


def wrap(text, width, size):
    font = ImageFont.truetype(str(FONT), size)
    lines, current = [], ""
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


def sequence(name, title, lanes, rows):
    row_height = 60 if len(rows) > 12 else 76
    height = 230 + row_height * len(rows)
    f = Figure(name, title, height=height)
    f.sequence_rows, f.sequence_lanes, f.sequence_messages = len(rows), lanes, rows
    spacing = 800 / (len(lanes) - 1)
    centers = [100 + spacing * i for i in range(len(lanes))]
    header_w = min(190, spacing - 24)
    for index, (center, (title, subtitle)) in enumerate(zip(centers, lanes, strict=True)):
        f.node(f"lane-{index}", center - header_w / 2, 85, header_w, 72, title, [subtitle], size=20)
        f.parts.append(
            f'<path d="M{center},157 V{height - 20}" fill="none" '
            'stroke="#B7C1CD" stroke-width="1.3" stroke-dasharray="4 6"/>'
        )
    for index, (identity, source, target, label, kind) in enumerate(rows):
        y = 225 + index * row_height
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
            outer = a + direction * 56
            f.arrow([(a, y), (outer, y), (outer, y + 20), (a, y + 20)], color=color)
            lines = wrap(f"{identity} {label}", spacing - 30, 20)
            text_x = a + direction * (spacing / 2 - 9)
        else:
            f.arrow([(a, y + 20), (b, y + 20)], color=color, dash=kind == "return")
            lines = wrap(f"{identity} {label}", spacing - 25, 20)
            text_x = a + (1 if b > a else -1) * spacing / 2
        assert len(lines) <= 2, (name, identity, lines)
        for number, line in enumerate(lines):
            f.text(text_x, y - 10 - (len(lines) - 1 - number) * 24, line, 20, color=color)
    return f


def sequences():
    return [
        sequence(
            "customer-access-v1.2",
            "고객 인증과 일반·AAS 조회",
            [
                ("고객", "직원·파트너"),
                ("Cognito", "회사 IdP 연동"),
                ("보호 API", "Gateway·Lambda"),
                ("PostgreSQL", "권한·자산·이벤트"),
            ],
            [
                ("Q01", 0, 1, "로그인·토큰 요청", "api"),
                ("Q02", 1, 0, "access token", "return"),
                ("Q03", 0, 2, "조회 요청", "api"),
                ("Q04", 2, 2, "토큰·scope 검사", "api"),
                ("Q05", 2, 3, "주체·자산 권한", "api"),
                ("Q06", 3, 2, "허용 데이터", "return"),
                ("Q07", 2, 2, "일반·AAS 조립", "api"),
                ("Q08", 2, 0, "조회 응답", "return"),
            ],
        ),
        sequence(
            "replication-flow-v1.2",
            "이벤트 접수와 NAS 복제 확인",
            [
                ("현장 엣지", "outbox·관리 사본"),
                ("AWS API", "DB·전송 권한"),
                ("사무실 수신기", "manifest"),
                ("사무실 NAS", "검증 클립"),
            ],
            [
                ("R01", 0, 0, "로컬 접수 확정", "api"),
                ("R02", 0, 1, "이벤트 전송", "api"),
                ("R03", 1, 0, "DB 저장 ACK", "return"),
                ("R04", 0, 1, "현장 사본 등록", "api"),
                ("R05", 0, 1, "전송 준비", "api"),
                ("R06", 1, 0, "수신 권한·URL", "return"),
                ("R07", 0, 2, "verify·클립 PUT", "media"),
                ("R08", 2, 1, "수신 권한 확인", "api"),
                ("R09", 2, 3, "검증·원자 확정", "media"),
                ("R10", 2, 1, "완료 보고", "api"),
                ("R11", 1, 2, "STORED 확인", "return"),
                ("R12", 2, 0, "복제 receipt", "return"),
            ],
        ),
        sequence(
            "clip-flow-v1.2",
            "클립 요청과 전달 전체 절차",
            [
                ("고객", "직원·파트너"),
                ("AWS API", "DB·SQS"),
                ("선택 업로더", "사무실·현장"),
                ("검증 사본", "NAS·캐시"),
                ("비공개 S3", "시도별 객체"),
            ],
            [
                ("C01", 0, 1, "클립 요청", "api"),
                ("C02", 1, 1, "작업·큐 확정", "api"),
                ("C03", 1, 0, "202·작업 ID", "return"),
                ("C04", 2, 1, "작업 claim", "api"),
                ("C05", 1, 1, "단일 lease", "api"),
                ("C06", 1, 2, "작업·lease", "return"),
                ("C07", 2, 3, "검증·읽기", "media"),
                ("C08", 2, 1, "업로드 URL", "api"),
                ("C09", 1, 2, "서명 PUT URL", "return"),
                ("C10", 2, 4, "직접 PUT", "media"),
                ("C11", 2, 1, "완료 보고", "api"),
                ("C12", 1, 4, "HEAD 검증", "api"),
                ("C13", 1, 1, "READY 확정", "api"),
                ("C14", 0, 1, "상태 조회", "api"),
                ("C15", 1, 0, "서명 GET URL", "return"),
                ("C16", 0, 4, "직접 GET", "media"),
            ],
        ),
        sequence(
            "failover-flow-v1.2",
            "사무실 장애와 현장 예비 전환",
            [
                ("AWS 제어", "API·유지관리"),
                ("사무실 워커", "주 업로더"),
                ("해당 현장", "예비 업로더"),
                ("비공개 S3", "시도별 객체"),
            ],
            [
                ("H01", 1, 0, "비정상·실패 보고", "api"),
                ("H02", 0, 0, "기한·상태 점검", "api"),
                ("H03", 0, 0, "lease 보호·만료", "api"),
                ("H04", 0, 0, "유효 사본 선택", "api"),
                ("H05", 2, 0, "새 배정 claim", "api"),
                ("H06", 0, 2, "새 lease·URL", "return"),
                ("H07", 2, 3, "현장 직접 PUT", "media"),
                ("H08", 1, 0, "이전 시도 통지", "api"),
                ("H09", 0, 1, "404·409 거부", "reject"),
                ("H10", 2, 0, "완료·실물 검증", "api"),
            ],
        ),
        sequence(
            "retention-flow-v1.2",
            "30일 현장 사본 정리",
            [
                ("현장 엣지", "로컬 사본·잠금"),
                ("사무실 수신기", "전송 재검증"),
                ("사무실 NAS", "동일 클립"),
                ("AWS API", "사본·작업 잠금"),
            ],
            [
                ("T01", 0, 0, "보관 기간 도래", "api"),
                ("T02", 0, 1, "verify 요청", "api"),
                ("T03", 1, 2, "바이트 재검증", "media"),
                ("T04", 1, 3, "동일 사본 확인", "api"),
                ("T05", 1, 0, "STORED receipt", "return"),
                ("T06", 0, 3, "retire 요청", "api"),
                ("T07", 3, 3, "건강·작업 검사", "api"),
                ("T08", 3, 0, "RETIRED 확정", "return"),
                ("T09", 0, 0, "RETIRING·삭제", "api"),
            ],
        ),
    ]


def operations():
    f = Figure("aws-operations-v1.2", "AWS 네트워크와 운영 연결", height=1240)
    f.panel(20, 90, 960, 460, "VPC 네트워크")
    f.panel(20, 570, 960, 270, "유지관리")
    f.panel(20, 870, 960, 350, "관측·감사")
    f.node("functions", 50, 155, 230, 72, "API·유지관리", ["private Lambda"])
    f.node("rds", 670, 155, 280, 72, "RDS PostgreSQL", ["private·TLS"])
    f.node("nat", 365, 310, 250, 72, "NAT Gateway", ["개발 단일 NAT"])
    f.node("services", 690, 310, 260, 72, "AWS HTTPS", ["SQS·Cognito·Secrets"])
    f.node("endpoint", 365, 455, 250, 72, "S3 Endpoint", ["Gateway 유형"])
    f.node("s3", 690, 455, 260, 72, "ClipBucket", ["HEAD·DELETE"])
    f.node("timer", 50, 630, 230, 72, "EventBridge", ["1분 주기"])
    f.node("maintenance", 365, 630, 250, 72, "유지관리 Lambda", ["재배정·만료"])
    f.node("state", 690, 630, 260, 72, "PostgreSQL·SQS", ["상태·배정 갱신"])
    f.node("cleanup", 690, 750, 260, 72, "이전 S3 객체", ["안전 기한 후 삭제"])
    f.node("sources", 50, 945, 230, 72, "Gateway·Lambda", ["접근·실행 추적"])
    f.node("cw", 365, 945, 250, 72, "CloudWatch·X-Ray", ["로그·지표·추적"])
    f.node("alarms", 690, 945, 260, 72, "경보", ["오류·용량·격리"])
    f.node("audit-source", 50, 1100, 230, 72, "ClipBucket", ["접근 감사"])
    f.node("audit", 365, 1100, 250, 72, "AuditBucket", ["접근·감사 로그"])
    f.node("trail", 690, 1100, 260, 72, "CloudTrail", ["managed 모드"])
    f.edge("E02", "functions", "rds", [(280, 191), (670, 191)], "SQL·TLS", (475, 175))
    f.edge(
        "E04",
        "functions",
        "nat",
        [(165, 227), (165, 346), (365, 346)],
        "egress",
        (255, 280),
        color=GRAY,
    )
    f.edge("E05", "nat", "services", [(615, 346), (690, 346)], "", (652, 328), color=GRAY)
    f.edge(
        "E06a",
        "functions",
        "endpoint",
        [(100, 227), (100, 491), (365, 491)],
        "S3",
        (205, 450),
        color=ORANGE,
    )
    f.edge("E06b", "endpoint", "s3", [(615, 491), (690, 491)], "", (652, 473), color=ORANGE)
    f.edge("E01", "timer", "maintenance", [(280, 666), (365, 666)], "", (322, 648))
    f.edge("E03", "maintenance", "state", [(615, 666), (690, 666)], "", (652, 648))
    f.edge(
        "E08",
        "maintenance",
        "cleanup",
        [(490, 702), (490, 786), (690, 786)],
        "정리",
        (590, 765),
        color=ORANGE,
    )
    f.edge("E07a", "sources", "cw", [(280, 981), (365, 981)], "", (322, 962), color=GRAY)
    f.edge("E07b", "cw", "alarms", [(615, 981), (690, 981)], "", (652, 962), color=GRAY)
    f.edge("E10", "audit-source", "audit", [(280, 1136), (365, 1136)], "", (322, 1118), color=GRAY)
    f.edge(
        "E11", "trail", "audit", [(690, 1136), (615, 1136)], "", (652, 1118), color=GRAY, dash=True
    )
    return f


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("docs/diagrams"))
    args = parser.parse_args()
    FONT = args.font
    args.output.mkdir(parents=True, exist_ok=True)
    steps = sequences()
    clip = steps[2]
    request = sequence(
        "clip-request-v1.2",
        "클립 요청과 업로드 배정",
        clip.sequence_lanes,
        clip.sequence_messages[:9],
    )
    complete = sequence(
        "clip-completion-v1.2",
        "클립 업로드 검증과 다운로드",
        clip.sequence_lanes,
        clip.sequence_messages[9:],
    )
    figures = [overall(), backend(), aas(), media(), *steps, operations(), request, complete]
    print(
        json.dumps(
            [f.save(args.output, validate=bool(f.edges)) for f in figures],
            ensure_ascii=False,
            indent=2,
        )
    )
