# 실행 로그 운영

## 공통 처리

Python 표준 logging과 공통 데코레이터로 기능 실행 전후에 로그를 남긴다. `@logged`는 동기·비동기 함수와 자원 수명을 갖는 generator를 감싸고, `@log_methods`는 클래스의 공개 메서드에 적용한다. API는 `LoggedRoute`로 등록하므로 새 경로에도 같은 실행 로그가 붙는다. HTTP 미들웨어는 인증·입력 검증·본문 크기 제한·미등록 경로·예기치 못한 예외를 포함해 요청 종료를 기록한다.

로그 저장 실패가 업무 처리의 반환값이나 예외를 바꾸지 않도록 한다. 기존 DB 감사 이력과 S3 접근 감사는 각각의 목적에 따라 유지한다.

| 적용 지점 | 기본 실행 로그 수준 | 별도 상태 기록 |
|---|---|---|
| 전체 API 경로, 이벤트·미디어·이력·현재값, 클립·lease 관리 | INFO | HTTP 결과, 거부 사유 코드, heartbeat 상태 |
| AAS 어댑터, 인증·권한 검사, AWS S3·SQS 접근 | DEBUG | 실패는 WARNING 또는 ERROR |
| 엣지 로컬 접수, 이벤트·미디어 실제 전송, 클립 업로드 | INFO | 접수 수, outbox 상태, 재시도 지연, 격리, 클립 실패·lease 상실 |
| 엣지 빈 inbox·outbox 폴링, SQLite·NAS 접근, HTTP·토큰 처리 | DEBUG | 실패는 WARNING 또는 ERROR |
| 등록·상태 조회·재처리 명령, 엣지 실행, 정기 유지관리 | INFO | 확인 작업 수, 삭제 객체 수 |

생성자, 입력 모델의 개별 검증기, 시각·해시·JSON 변환 같은 순수 보조 함수는 별도 실행 로그를 생성하지 않는다. 해당 실패는 이를 호출한 기능 경계에서 기록한다.

## 필드와 해석

| 필드 | 의미 |
|---|---|
| `timestamp`, `level`, `component`, `event`, `operation` | UTC 시각, 심각도, 실행 단위, 로그 종류, 기능 이름 |
| `traceId` | 서버가 생성한 요청 또는 실행 문맥 ID. HTTP 응답의 `X-Request-Id`와 일치 |
| `spanId`, `parentSpanId` | 중첩 함수 호출의 연결. 같은 함수의 시작·종료는 같은 span |
| `awsRequestId` | Lambda 실행 ID. 영상 작업 ID와 구분 |
| `backendTraceId` | 엣지가 받은 백엔드 응답의 `X-Request-Id`. 현장·서버 로그 연결에 사용 |
| `eventId`, `requestId`, `clipId`, `outboxId` | 이벤트, 영상 작업, 클립, 현장 전송 항목의 식별자 |
| `principal`, `edgeServerId`, `siteId`, `assetId` | 확인할 수 있는 처리 주체와 대상 식별자 |
| `durationMs`, `status`, `errorType`, `errorCode` | 소요시간, HTTP 결과, 예외 종류, 기계 판독용 오류 코드 |

식별자는 해당 기능에서 알 수 있을 때만 기록한다. HTTP 경로는 `/events/{event_id}` 같은 등록 패턴으로 기록하며 실제 쿼리 문자열과 알 수 없는 경로 원문은 수집하지 않는다. 호출자가 전송한 추적 헤더를 서버 추적 ID로 신뢰하지 않는다. 직접 만든 업로드·lease 스레드에는 실행 문맥을 복사하고 작업 종료 시 복원한다.

`operation_started`, `operation_succeeded`, `operation_failed`, `operation_cancelled`는 함수 실행 결과다. `operation_succeeded`는 예외 없이 반환했다는 의미다. 재시도를 예약하거나 실패 보고를 정상적으로 끝낸 함수도 여기에 해당하므로 업무 결과는 `outbox_state_changed`, `clip_job_failed`, `job_lease_lost` 등 상태 로그로 판단한다. 예상한 4xx 거부는 WARNING, 처리 불가능한 예외와 5xx는 ERROR로 기록한다.

로그 필드는 허용 목록으로 제한한다. 인자·반환값을 일괄 직렬화하거나 객체 repr을 호출하지 않는다. 요청/응답 본문, access token, client secret, lease token, cursor, 서명 URL, NAS 경로, SQL과 예외 메시지·원문 traceback은 애플리케이션 실행 로그에 넣지 않는다. DEBUG에서도 이 기준이 같다.

## 설정과 조회

백엔드는 `DSC_LOG_LEVEL`, 엣지는 `DSC_EDGE_LOG_LEVEL`을 사용한다. 허용값은 `DEBUG`, `INFO`, `WARNING`, `ERROR`이며 기본값은 `INFO`다. 운영 명령의 일반 결과는 stdout, JSON 실행 로그는 stderr로 분리한다. AWS 실행 시 Lambda 환경변수로 설정하며, 현장에서는 `edge.env` 수정 후 서비스를 재시작한다.

```sh
journalctl -u dsc-edge-server --since '30 minutes ago' -o cat
```

CloudWatch Logs Insights에서 요청을 추적하는 예:

```text
fields @timestamp, event, operation, traceId, eventId, requestId, errorCode, durationMs
| filter traceId = "확인할-traceId"
| sort @timestamp asc
```

영상 작업은 `requestId`로 서버·현장 양쪽에서 찾고, 엣지의 개별 통신은 `backendTraceId`를 서버 `traceId`와 대조한다. 실행 로그 보관·전송·열람 권한과 호스팅 서버 자체 로그는 운영 환경에서 관리한다.

## 기능 추가 시 적용

기존 `Platform`, `AasAdapter`, AWS 어댑터, `EdgeClient`, `EdgeStore`에 공개 메서드를 추가하면 클래스 단위 로깅이 적용된다. 별도 기능 함수에는 `@logged`를 적용한다. 고빈도 내부 접근은 `level=logging.DEBUG`를 지정한다. generator 기반 context manager는 `@contextmanager` 아래에 `@logged`를 둬야 실제 자원 사용 기간이 기록된다.

추적용 인자 필드는 데코레이터의 `fields`에 명시한 식별자 경로만 읽는다. 민감정보를 새 필드로 추가하지 않는다. 직접 생성하는 스레드·executor에는 `contextual`을 사용한다. 업무상 실패를 처리하고 정상 반환하는 경로는 공통 `emit_event`로 결과 코드와 상태를 남긴다.
