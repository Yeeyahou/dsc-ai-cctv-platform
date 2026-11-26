# DSC AI CCTV 플랫폼

퓨처렉스 DSC 센터의 이벤트를 수집·조회하고 요청한 녹화 클립을 전달하는 Python 백엔드, CCTV·로봇 공통 엣지 서버와 사무실 업로드 워커다. 구현 계약은 [연동 규격서](docs/aws-backend-edge-server-spec-v1.3.md)를 따른다.

## 구현 구성

| 구분 | 구성 |
|---|---|
| Python | 3.12, 공통 입력 모델 Pydantic 2 |
| 백엔드 | FastAPI, Mangum, API Gateway REST API, Lambda |
| 인증 | Cognito access token, 회사 SAML/OIDC 연동, M2M client credentials |
| 데이터 | PostgreSQL 17, SQLAlchemy 2, Alembic |
| 영상 작업 | 설치별 SQS 큐, DB lease, 검증 사본별 주·예비 배정, S3 서명 PUT/GET URL |
| 현장 엣지 서버 | HTTPX, SQLite WAL outbox·manifest, POSIX 파일 접근, Linux systemd |
| 운영 | SAM/CloudFormation, WAF, CloudWatch, S3 접근 로그·CloudTrail 데이터 이벤트 |
| 검증 | pytest, Ruff, cfn-lint, CloudFormation Guard 규칙 |

DB는 이벤트·자산 관계와 작업 lease를 같은 트랜잭션으로 관리하기 위해 PostgreSQL을 선택했다. 규격의 미정 구현 선택과 운영 조건은 [구현 결정](docs/implementation-decisions.md)에 정리했다.

전체 구성, 백엔드 내부 기능, 고객의 이벤트·시계열·AAS 조회, NAS 복제, 클립 요청·다운로드, 장애 전환·정리 및 AWS 배포·관측을 10개 관점의 11개 도식으로 정리했다. 노드 문구는 두 줄 이하로 제한하고 상세 내용은 별도 노드·연결과 본문 대응표로 설명한다. [시스템 아키텍처 상세 설계](docs/system-architecture-v1.2.md)와 Word 문서는 기존 A4 세로 양식을 유지한다.

## 실시간 CCTV와 NAS 설치 준비

사무실 브라우저의 회사 계정 로그인과 카메라별 WebRTC 시청을 추가했다. 현장에서는
MediaMTX 1.21.4와 Python 인증 감시를 별도 컨테이너로 실행하며 VPN 안에서만 연결한다.
기본 5분 세션, 권한 회수·만료 때 기존 연결 종료, 시청 종료·영상 정지 안내를 제공한다.
라이브는 기본 비활성이고 기존 이벤트·NAS·S3·AAS 계약은 유지한다.

- [v1.3 전체 규격](docs/aws-backend-edge-server-spec-v1.3.md)과 같은 이름의 Word
- [실시간 아키텍처와 기존 기능 대응](docs/system-architecture-v1.3.md) 및 Word
- [스트리밍 설치·로그인·VPN·복구](docs/streaming-installation-guide.md) 및 Word
- [실시간 API 요청·응답과 예제](docs/live-api-v1.3.md) 및 Word
- [장비 기록·사전 점검·NAS 설치 당일 인수](docs/installation-readiness.md) 및 Word

설치 파일은 `deploy/streaming/`, 카메라 등록 예제는 `examples/live-registration.json`이다.
새 DB 마이그레이션과 runtime grants, Cognito live scope·callback·직원 권한, 실제 VPN·TLS·
카메라 계정 설정 후 활성화한다. 새 버전의 AWS 배포·물리 NAS·CCTV 설치 검수는 별도다.

```sh
pytest tests/test_live.py tests/test_streaming_gateway.py
PYTHONPATH=src python -m dsc.devtools.preflight --help
python scripts/build_install_bundle.py runtime/install-ready/NEW_RUN
```

합성 RTSP→실제 MediaMTX→Chrome 시험은 `scripts/check_streaming.py`와
`check_streaming_browser.cjs`를 사용한다. Docker·Playwright·브라우저를 준비하고
[설치 안내](docs/streaming-installation-guide.md)의 실행 조건을 따른다.

## 브랜치 운영

기본 개발 브랜치는 `develop`이다. 최신 `develop`에서 기능별 `codex/feature/...` 또는 수정·설정 작업 브랜치를 만들고, PR과 자동 검사를 거쳐 `develop`에 통합한다. 검증과 배포 승인이 끝난 코드는 `develop → main` PR로 승격한다. `main`과 `develop`에 직접 푸시하지 않는다. 브랜치 보호, 병합 방식과 승인 기준은 [브랜치 운영 안내](docs/branching.md)를 따른다.

## 개발 환경

Python 3.12와 Docker Desktop 또는 Docker Engine을 준비한다. 테스트용 PostgreSQL은 `127.0.0.1:55432`에서만 열린다.

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-build-isolation --no-deps -e .
make lint
pytest
docker compose up -d --wait postgres
DSC_TEST_POSTGRES_URL=postgresql+psycopg://dsc_test:local-test-only@127.0.0.1:55432/dsc_test pytest
```

`DSC_TEST_POSTGRES_URL`로 지정하는 DB는 **테스트가 테이블을 삭제하고 다시 만드는 전용 DB**다. 운영·개발 데이터가 든 DB를 지정하지 않는다. 컨테이너 데이터는 임시 저장소에 있으며 테스트 후 `docker compose down`으로 종료한다.

개발 서버에서도 Cognito 서명을 검증한다. 인증 우회나 공용 테스트 계정은 제공하지 않는다. 로컬에서 전체 API를 호출하려면 개발용 AWS 계정의 Cognito, S3, SQS와 등록 정보를 설정한다. 기본 자동 테스트는 외부 AWS 호출을 격리한다.

```sh
cp examples/backend.env.example .env
# .env를 개발 환경 값으로 수정하고 현재 셸에 적용한다.
set -a
. ./.env
set +a
alembic upgrade head
dsc-admin register deployment.local.json
uvicorn dsc.backend.api:create_app --factory --host 127.0.0.1 --port 8000
```

환경 파일의 JSON 배열 값은 셸에서 읽을 경우 작은따옴표로 감싸야 한다. 셸 적용 대신 `uvicorn --env-file .env`와 `dsc-admin --env-file .env`를 사용할 수도 있다. Alembic은 실행 프로세스의 환경 변수를 읽는다.

`DSC_ENVIRONMENT=local`에서는 [Swagger UI](http://127.0.0.1:8000/docs)로 API 목록과 요청 형식을 볼 수 있다. 보호된 API를 호출할 때는 기존과 동일하게 유효한 Cognito 토큰이 필요하다.

## 주요 기능

- `/ingest`: 원자적 이벤트 저장, 동일 ID 재수신 확인, 다른 본문 충돌 거부
- `/events`, `/timeseries`: 기간·자산별 이력, 권한 필터 후 집계, 서명된 페이지 cursor
- `/shells`, `/submodels`, Registry·Discovery 읽기: 같은 자산·이력 DB에 대한 AAS 어댑터
- `/files/.../requests`, `/file-requests`: 클립 요청 중복 제한, 상태 조회, 만료 전용 다운로드 링크
- `/edge-server/jobs/...`: 작업 claim·lease 연장·조건부 업로드·완료 검증·실패 재배정
- `/edge-server/replicas`, `/edge-server/archive/...`: 검증 사본 등록, 전송 권한, NAS 저장 확인과 정리 보호
- `/edge-server/heartbeat`: 엣지 서버 관측 시각과 서버 수신 시각을 구분한 상태 보고

센터 전체 또는 이벤트에 연결된 모든 자산의 읽기 권한을 확인한다. 일부 자산만 허용된 사용자는 해당 이벤트 전체·클립을 볼 수 없다. 파트너의 이력·영상 접근은 별도로 등록한다.

클립은 사무실 NAS에 보관하고 현장 서비스 전용 영역에 기본 30일의 예비 사본을 유지한다. 사무실 워커를 우선하고 장애 시 검증된 현장 사본으로 업로드한다. 요청한 기본 클립만 S3에 업로드하며 엣지 서버에 AWS 장기 자격 증명을 넣지 않는다. 클립 요청은 30분 안에서 최대 3회 시도하고 lease를 60초마다 연장한다. 완료 시 실제 크기와 SHA-256을 대조한다. 업로드 URL 15분, 다운로드 URL 최대 5분, READY 이후 접근 보관 24시간이 기본값이다.

## 배포와 현장 설치

AWS 배포·DB 계정·Cognito 회사 계정 연동·등록 절차는 [배포 및 운영 안내](docs/deployment.md)를 따른다. 배포 전 개발 환경에서 외부 연동 시험을 수행한다. 커밋·푸시와 AWS 리소스 배포는 별도 작업이다.

FutureX의 개발 역할 경계에 맞춘 신규 SAM 배포는 [개발 환경 배포 안내](docs/dev-deployment.md)를 따른다.

v1.0 설치를 갱신할 때는 [v1.1 연동 변경 이력과 전환 절차](docs/edge-server-upgrade-v1.1.md)를 확인한다.

신규 CCTV·로봇 현장은 `examples/field.env.example`, 사무실 수신기·워커는 `examples/office.env.example`을 사용한다. 설치·HTTPS·NAS 마커·30일 관리 사본 정리와 장애 복구는 [사무실·현장 운영 안내](docs/office-worker-operations.md)를 따른다. 기존 legacy 설치는 `examples/edge.env.example`과 기존 서비스 파일을 유지할 수 있다. 비밀 파일 권한은 `0600`으로 두고 업데이트 시 outbox·manifest·관리 사본 경로를 보존한다.

맥북에서 실제 MP4와 목업 이벤트를 Docker로 접수하고 컨테이너 재시작·재생성 후 DB·클립을
검증하는 방법은 [현장 Docker 실행 안내](docs/field-docker-guide.md)에 있다.
`scripts/check_field_docker.py --video /absolute/path/to/clip.mp4 --duration-seconds 25.174169`는
네트워크 차단 개발용 컨테이너를 사용한다. 운영용 `compose.field.yaml`은 별도 설치 인증과
사무실 수신 설정을 준비한 뒤 사용한다. 두 구성 모두 기존 공통 Docker 이미지를 사용한다.

2026년 11월 11일 AWS 개발 스택을 v1.2로 갱신하고 실제 AWS 업로드·장애 전환·다운로드 만료를 확인했다. 배포 해시와 데이터 보존 및 남은 물리 장비 검수는 [개발 배포 기록](docs/office-worker-deployment-v1.2.md)에 있다. 현장·사무실 장비 설치는 NAS와 접속·네트워크 정보가 준비된 뒤 수행한다.

```sh
dsc-edge-server --env-file /etc/dsc/edge.env
dsc-edge-server --env-file /etc/dsc/edge.env --inspect-outbox
# 원인 조치 후 격리 건을 명시적으로 재처리한다.
dsc-edge-server --env-file /etc/dsc/edge.env --retry 123
```

로컬 AI·녹화 프로그램은 [inbox 예제](examples/inbox-event.json)를 참고해 `.tmp`로 작성·동기화 후 같은 파일시스템에서 `{eventId}.event.json`으로 이름을 변경한다. 클립 상태 갱신은 `{eventId}.{revision}.media.json`으로 전달한다. `localMedia.relativePath`는 현장에서만 저장하며 서버로 보내지 않는다. accepted 이동은 로컬 접수 완료이며 서버 저장 완료는 outbox의 ACKED로 확인한다.

## 분석 프로그램 연동 준비

생산자 담당자에게 전달할 [분석·녹화 연동 계약](docs/producer-integration-contract.md)과
[실행 목업 안내](docs/producer-mock-guide.md)를 제공한다. `PYTHONPATH=src python -m dsc.devtools.producer`는
CCTV·로봇별 합성 MP4와 기존 inbox 형식의 JSON을 만들며, `--replay`로 같은 입력을 재전송한다.
목업은 개발 도구이며 별도 HTTP 입력 API나 운영 인증 우회를 추가하지 않는다.

[외부 AAS 소비자 목업](docs/aas-consumer-guide.md)은 개발용 파트너 자격 증명으로
Discovery·Registry·Shell·Submodel·$value와 금지 자산의 접근 거부를 검사하고 JSON 결과를 남긴다.

전체 실행 순서는 [장비 도착 전 준비 작업](docs/preparation-work-guide.md)에 있다.
[장애 재현](docs/fault-rehearsal-guide.md)은 실제 loopback 소켓의 유실·지연과 별도 프로세스의
강제 종료 복구를 검증한다. [검수 보고서 도구](docs/acceptance-report-guide.md)는
AT01~AT25를 pytest JUnit 결과와 연결해 `report.json`·`report.md`를 만든다.
실제 장비·AWS·파트너 검수에는 [기록 양식](docs/acceptance-report-template.md)을 함께 사용한다.
각 세부 안내에는 같은 이름의 Word 문서도 제공한다. 기존 규격서와 아키텍처 Word는 재생성하지 않는다.

## 실행 로그

API 전체와 백엔드·엣지의 기능 실행에 공통 AOP 로깅을 적용한다. 시작·정상 반환·예외·소요시간을 JSON으로 기록하고, `traceId`와 호출별 `spanId`로 연결한다. 이벤트의 `eventId`, 영상 작업의 `requestId`, Lambda의 `awsRequestId`는 별도 필드다. 로그는 stderr로 출력하며 Lambda에서는 CloudWatch, systemd에서는 journal에서 확인한다.

기본값은 INFO다. 상세 함수·저장소·외부 통신·반복 폴링 로그는 백엔드 `DSC_LOG_LEVEL=DEBUG`, 엣지 `DSC_EDGE_LOG_LEVEL=DEBUG`로 활성화한다. 두 설정은 각각의 환경 파일에서도 읽는다. 토큰·비밀번호·lease 토큰·서명 URL·NAS 경로·요청/응답 본문·예외 메시지는 애플리케이션 로그에 수집하지 않는다. 적용 지점, 상태 로그와 조회 방법은 [로그 운영 안내](docs/logging.md)에 정리했다.

## 산출물과 검증

엣지·사무실 워커 개발자를 위한 [AWS API 명세서](docs/edge-cloud-api-v1.2.md)와 같은 이름의
Word를 제공한다. AWS 제어 API 13개의 요청·응답·오류·cURL 예제, M2M 인증, 연관 사무실
수신기·S3 전송 및 재시도 순서를 정리했다. 예제 JSON은 `examples/edge-cloud-api/`에 있다.
`PYTHONPATH=src python scripts/check_edge_api_reference.py`로 경로·요청 모델·예제와 코드 사전을
대조한다. Word 재생성은 python-docx·Pillow가 있는 문서 작성 환경에서
`python scripts/build_edge_api_reference.py`를 실행하며 기존 규격서 Word는 수정하지 않는다.

자체 API OpenAPI는 `docs/openapi.json`, DB 변경은 `migrations/`, AAS 고정 스키마·필드 매핑은 `src/dsc/backend/schemas/`에 있다. IDTA 원본의 출처·해시는 `sources.json`, 라이선스는 `IDTA-LICENSE.txt`에서 확인한다.

```sh
make openapi
alembic check
cfn-lint infra/template.yaml infra/database.yaml
cfn-guard validate --rules infra/security.guard --data infra/template.yaml --output-format json
cfn-guard validate --rules infra/security.guard --data infra/database.yaml --output-format json
```

검수 항목별 자동 시험과 실제 AWS 연동 범위는 [검증 기록](docs/verification.md)을 참고한다. 실제 회사 IdP, 현장 NAS·회선·전원, 부하와 재해 복구 시험은 운영 환경값과 함께 확인해야 한다.
