# DSC AI CCTV 플랫폼

퓨처렉스 DSC 센터의 이벤트를 수집·조회하고 요청한 녹화 클립을 전달하는 Python 백엔드, CCTV·로봇 공통 엣지 서버와 사무실 업로드 워커다. 구현 계약은 [연동 규격서](docs/aws-backend-edge-server-spec-v1.2.md)를 따른다.

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

```sh
dsc-edge-server --env-file /etc/dsc/edge.env
dsc-edge-server --env-file /etc/dsc/edge.env --inspect-outbox
# 원인 조치 후 격리 건을 명시적으로 재처리한다.
dsc-edge-server --env-file /etc/dsc/edge.env --retry 123
```

로컬 AI·녹화 프로그램은 [inbox 예제](examples/inbox-event.json)를 참고해 `.tmp`로 작성·동기화 후 같은 파일시스템에서 `{eventId}.event.json`으로 이름을 변경한다. 클립 상태 갱신은 `{eventId}.{revision}.media.json`으로 전달한다. `localMedia.relativePath`는 현장에서만 저장하며 서버로 보내지 않는다. accepted 이동은 로컬 접수 완료이며 서버 저장 완료는 outbox의 ACKED로 확인한다.

## 실행 로그

API 전체와 백엔드·엣지의 기능 실행에 공통 AOP 로깅을 적용한다. 시작·정상 반환·예외·소요시간을 JSON으로 기록하고, `traceId`와 호출별 `spanId`로 연결한다. 이벤트의 `eventId`, 영상 작업의 `requestId`, Lambda의 `awsRequestId`는 별도 필드다. 로그는 stderr로 출력하며 Lambda에서는 CloudWatch, systemd에서는 journal에서 확인한다.

기본값은 INFO다. 상세 함수·저장소·외부 통신·반복 폴링 로그는 백엔드 `DSC_LOG_LEVEL=DEBUG`, 엣지 `DSC_EDGE_LOG_LEVEL=DEBUG`로 활성화한다. 두 설정은 각각의 환경 파일에서도 읽는다. 토큰·비밀번호·lease 토큰·서명 URL·NAS 경로·요청/응답 본문·예외 메시지는 애플리케이션 로그에 수집하지 않는다. 적용 지점, 상태 로그와 조회 방법은 [로그 운영 안내](docs/logging.md)에 정리했다.

## 산출물과 검증

자체 API OpenAPI는 `docs/openapi.json`, DB 변경은 `migrations/`, AAS 고정 스키마·필드 매핑은 `src/dsc/backend/schemas/`에 있다. IDTA 원본의 출처·해시는 `sources.json`, 라이선스는 `IDTA-LICENSE.txt`에서 확인한다.

```sh
make openapi
alembic check
cfn-lint infra/template.yaml infra/database.yaml
cfn-guard validate --rules infra/security.guard --data infra/template.yaml --output-format json
cfn-guard validate --rules infra/security.guard --data infra/database.yaml --output-format json
```

검수 항목별 자동 시험과 실제 환경 확인 범위는 [검증 기록](docs/verification.md)을 참고한다. 실제 회사 IdP, 현장 NAS, AWS 통합·부하·재해 복구 시험은 운영 환경값과 함께 확인해야 한다.
