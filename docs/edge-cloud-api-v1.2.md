# 현장 엣지 및 사무실 워커 AWS API 명세서

문서 버전 1.2　작성일 2026년 11월 16일　대상 CCTV·로봇 현장 엣지 및 사무실 수신기·업로드 워커

이 명세서는 현장·사무실 프로그램 개발자가 AWS 백엔드에 연동할 때 사용하는 HTTP API 계약이다. 인증, 요청과 응답, 오류, 재전송 및 호출 순서를 정의한다. 기존 연동 규격 v1.2의 계약을 유지하며 현재 OpenAPI와 구현을 대조한다. 예제의 주소·ID·토큰·서명·크기·해시는 설명용 값이다. 실제 등록값과 파일에서 계산한 값을 사용한다.

## 1 문서 관리와 적용 범위

| 항목 | 내용 |
|---|---|
| 문서 식별 | DSC EDGE CLOUD API 001 |
| 계약 버전 | 연동 규격 v1.2. 이벤트 schemaVersion 1.0, heartbeat 1.1. |
| 기준 문서 | aws-backend-edge-server-spec-v1.2.md 3~6장, 8~10장. |
| 기계 명세 | openapi.json. 요청 스키마를 사용하며 응답·업무 오류는 이 명세의 표와 예제를 함께 따른다. |
| 범위 | AWS 제어 API 13개. 인증과 연관 전송 인터페이스는 별도 절. |
| 관련 고객 API | 이벤트·시계열·AAS 조회 및 클립 요청·상태·다운로드는 기존 통합 규격 7~8장 참조. |

AI·녹화 프로그램의 로컬 inbox는 별도의 파일 계약이다. 이 HTTP 명세의 요청 본문에는 kind·event 래퍼, localMedia, NAS 상대 경로를 넣지 않는다. AWS JSON API, 사무실 영상 수신기, S3 PUT은 호출 대상과 인증 방식이 다르다.

### 1 1 역할과 권한

| 역할 | 책임 | 호출 권한 |
|---|---|---|
| field | CCTV·로봇 이벤트 발신, NAS 복제, 현장 예비 업로드 | ingest.write 및 edge-server.work |
| archive | 사무실 수신·검증·완료 보고와 S3 주 업로드 | edge-server.work. ingest.write 불필요. |
| legacy | 기존 설치의 이벤트·작업 처리 | 등록 scope에 따른 기존 동작. 신규 사본 배정 추가 필드 생략 가능. |

표의 scope는 모두 dsc/ 접두사를 포함한다. CCTV와 로봇은 설치마다 다른 client_id·엣지 ID·자산 허용 목록·큐·로컬 상태를 사용한다. 서버가 인증 주체로 설치를 결정하며, 요청자가 임의의 edgeServerId로 다른 설치를 선택하지 않는다.

### 1 2 찾아보기

2장 접속·인증 / 3장 공통 형식·오류 / 4장 API 목록 / 5장 API 상세 / 6장 연관 영상 전송 / 7장 호출 순서·복구 / 8장 데이터 사전·기본값 / 9장 예제 사용과 검증

## 2 접속 주소와 인증

### 2 1 접속 주소

| 변수 | 의미 |
|---|---|
| API_BASE_URL | AWS JSON API 기준 주소. 규격 기준 https://dsc.f2x.ai. 실제 배포에서는 제공된 API Gateway stage 또는 연결 완료된 custom domain 사용. |
| TOKEN_URL | 설치 Cognito 도메인의 /oauth2/token. API_BASE_URL과 별도. |
| ARCHIVE_URL | 사무실 수신기 HTTPS 주소. EC05의 uploadUrl에 포함. |
| S3 uploadUrl | EC11에서 반환된 이번 시도의 객체별 서명 URL. |

실제 설치 주소는 배포 매니페스트에서 확정한다. 규격 도메인이나 example.invalid 예제가 현재 접속 가능한 배포 주소임을 뜻하지 않는다. 모든 운영 연결은 HTTPS이며 현장에서 시작하는 아웃바운드 연결을 사용한다.

### 2 2 설치 토큰 발급

Cognito TOKEN_URL에 POST한다. Content-Type은 application/x-www-form-urlencoded, 인증은 해당 설치의 client_id·client secret에 대한 HTTP Basic이다. 본문은 grant_type=client_credentials와 scope이며 JSON이 아니다. field는 dsc/ingest.write dsc/edge-server.work, archive는 dsc/edge-server.work를 요청한다.

```http
POST ${TOKEN_URL}
Authorization: Basic <설치 client_id와 secret>
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&scope=dsc%2Fedge-server.work
```

성공 응답의 access_token을 사용하고 expires_in을 기준으로 갱신한다. ID token을 사용하지 않는다. 토큰 발급 오류는 Cognito 자체 형식이다. 등록 주체 비활성·scope·센터·자산 권한도 서버에서 검사하므로 토큰 발급 성공만으로 API 권한이 생기지 않는다.

```json
{
  "access_token": "EXAMPLE_ACCESS_TOKEN",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

위 expires_in은 예시다. 제품 엣지 클라이언트는 만료 60초 전부터 갱신하며 API 401이면 토큰을 새로 받아 한 번 더 호출한다. client secret은 설치 OS의 비밀 파일에 0600 등 제한 권한으로 저장한다. 이 문서와 예제 파일에는 실제 비밀을 넣지 않는다.

### 2 3 세 종류의 토큰 구분

| 토큰 | 전달 위치 | 사용 범위 |
|---|---|---|
| Cognito access token | AWS API의 Authorization: Bearer | 설치의 등록 scope·센터·자산 권한. |
| transferToken | AWS authorize·complete JSON 본문 / 사무실 수신기의 Bearer 헤더 | EC05가 발급한 전송 한 건의 정확한 콘텐츠와 발신·수신. |
| leaseToken | AWS 작업 lease·upload-url·complete·fail JSON 본문 | EC09가 배정한 requestId·담당자·시도·기한. |

사무실 수신기는 자신의 Cognito 토큰과 field가 전달한 transferToken을 함께 사용한다. S3 PUT에는 Cognito·transfer·lease Bearer 헤더를 보내지 않고 서명 URL과 반환 headers만 사용한다. 토큰·비밀번호·서명 URL 전체는 로그에 기록하지 않는다.

## 3 공통 요청과 응답

### 3 1 요청 규칙

| 항목 | 규칙 |
|---|---|
| HTTP 본문 | UTF-8 JSON 객체, Content-Type: application/json. claim도 빈 객체 {} 필요. |
| 인증 헤더 | Authorization: Bearer <설치 Cognito access token> |
| 날짜와 시각 | 시간대가 있는 RFC 3339. 전송 예제는 UTC Z 및 밀리초. 로컬 시각만 보내면 422. |
| 필드 이름 | 정의된 camelCase 그대로 사용. 정의되지 않은 요청 필드는 거부. |
| 선택 필드 | 값이 없으면 생략. null로 보내지 않음. |
| 수치 | 유한 수치만 허용. 정수 필드에 소수·문자열·boolean을 보내지 않음. |
| JSON 크기 | 최대 256 KiB. 영상 바이트는 별도 전송. |
| 경로 변수 | event_id·request_id·transfer_id는 UUID. 실제 URL에서는 중괄호 전체를 실제 ID로 치환. |
| 쿼리 파라미터 | 이 문서의 13개 AWS 작업에는 없음. |

OpenAPI의 일부 선택 필드 anyOf에 null 표기가 남아 있어도 실행 계약 모델은 null을 거부한다. 이 문서에서는 선택과 null 허용을 구분하며, 값이 없는 필드는 생략한다. AVAILABLE 추가 필드·사건 코드·지표 범위 등 모델 간 조건은 아래 업무 규칙까지 적용한다.

### 3 2 공통 응답 헤더와 오류

백엔드를 경유한 응답은 X-Request-Id, Cache-Control: no-store, X-Content-Type-Options: nosniff를 포함한다. 오류 traceId는 같은 X-Request-Id와 연결된다. API Gateway·WAF·Cognito·S3·프록시가 직접 거부한 응답은 이 형식을 보장하지 않으므로 먼저 HTTP 상태를 확인한다.

```json
{
  "error": {
    "code": "EVENT_ID_CONFLICT",
    "message": "동일 eventId에 다른 이벤트가 저장되어 있습니다",
    "traceId": "example-trace-id",
    "retryable": false
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| error.code | string | 프로그램이 분기할 업무 오류 코드. |
| error.message | string | 사람이 확인할 설명. 문자열 자체로 분기하지 않음. |
| error.traceId | string | 서버 요청 추적 ID. |
| error.retryable | boolean | 업무 측 재시도 가능성. 토큰·기한·충돌 조건을 함께 확인. |

| HTTP | 공통 코드 또는 의미 | 기본 처리 |
|---|---|---|
| 400 | INVALID_JSON | JSON 구문 수정. |
| 401 | AUTH_REQUIRED / INVALID_TOKEN / INACTIVE_PRINCIPAL | 토큰 갱신 후 1회 재시도. 지속되면 인증·등록 원인 조치. |
| 403 | INSUFFICIENT_SCOPE / EDGE_SERVER_REQUIRED | scope·설치 등록 확인. 자동 무한 반복 금지. |
| 404 | NOT_FOUND 등 업무별 코드 | 존재하지 않거나 해당 주체에 공개되지 않은 리소스. |
| 409 | 각 API의 충돌·만료 코드 | 내용·revision·lease·전송 상태에 따라 분기. |
| 413 | BODY_TOO_LARGE | 256 KiB 본문 상한 초과. |
| 422 | VALIDATION_ERROR | 필수·타입·UUID·상태·조건 검증 실패. |
| 429 | 요청 제한 | Retry-After가 있으면 따르고 backoff. |
| 503 | STORAGE_UNAVAILABLE / AWS_UNAVAILABLE | 외부 저장소 일시 실패. 같은 요청 정체성을 보존해 재시도. |
| 기타 5xx | 인프라·서버 오류 | 성공으로 기록하지 않고 요청 식별자·상태를 보존. |

### 3 3 재시도와 멱등성

이벤트 outbox는 201 또는 본문을 확인한 중복 200 후에만 ACKED로 바꾼다. 재시작으로 남은 SENDING은 PENDING으로 복구한다. 네트워크 오류·응답 불명·429·5xx에는 같은 ID·최초 본문으로 backoff를 적용한다. 기본 지연 상한은 60초이며 임의 지연을 섞고 Retry-After를 우선한다.

이벤트 outbox의 400·403·404·409·413·422는 일반적으로 격리 대상이다. 사본 복제에는 예외가 있다. ARCHIVE_NOT_CONFIGURED·TRANSFER_EXPIRED·REPLICA_IN_USE는 상태 조정·새 토큰·보존 조건을 확인한 뒤 재시도할 수 있다. 모든 409를 같은 방식으로 처리하지 않는다. 각 API 상세의 재요청 규칙을 따른다.

현재 업무 오류의 retryable은 기본 false이며 ARCHIVE_NOT_CONFIGURED·TRANSFER_EXPIRED·REPLICA_IN_USE 및 공통 STORAGE_UNAVAILABLE·AWS_UNAVAILABLE은 true다. 이 값만 보고 기존 토큰·본문을 무한 반복하지 않는다.

HTTP 성공과 파일 저장 성공을 구분한다. PRESENT는 현장 사본 등록, STORED는 NAS 저장 완료 기록, READY는 S3 검증 완료다. 세 상태를 서로 대체하지 않는다.

## 4 AWS 제어 API 목록

| ID | 메서드와 경로 | 호출 주체 |
|---|---|---|
| EC01 | POST /ingest | 발신 field 또는 기존 legacy 엣지 |
| EC02 | PUT /ingest/events/{event_id}/media | 해당 이벤트를 처음 전송한 엣지 |
| EC03 | POST /edge-server/heartbeat | 등록된 field·archive·legacy 설치 |
| EC04 | POST /edge-server/replicas | 이벤트 발신 field 엣지 |
| EC05 | POST /edge-server/archive/prepare | 이벤트 발신 field 엣지 |
| EC06 | POST /edge-server/archive/{transfer_id}/authorize | 해당 transferId에 지정된 사무실 수신기 |
| EC07 | POST /edge-server/archive/{transfer_id}/complete | 해당 transferId에 지정된 사무실 수신기 |
| EC08 | POST /edge-server/replicas/retire | 이벤트 발신 field 엣지 |
| EC09 | POST /edge-server/jobs/claim | 사무실 주 워커 또는 현장 예비 업로더 |
| EC10 | POST /edge-server/jobs/{request_id}/lease | 현재 유효 lease를 가진 업로더 |
| EC11 | POST /edge-server/jobs/{request_id}/upload-url | 현재 유효 lease를 가진 업로더 |
| EC12 | POST /edge-server/jobs/{request_id}/complete | 현재 유효 lease를 가진 업로더 |
| EC13 | POST /edge-server/jobs/{request_id}/fail | 현재 유효 lease를 가진 업로더 |

EC 번호는 이 문서의 API 참조 번호이며 실행 순서가 아니다. 공통 401·403·413·422·503 외에 API마다 추가 업무 오류가 있다. 토큰이 없는 호출, 잘못된 JSON·UUID 및 인프라 오류는 모든 API에 적용된다.

## 5 API 상세

### 5 1 EC01 이벤트 전송

```http
POST /ingest
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 발신 field 또는 기존 legacy 엣지 |
| 필요 scope | dsc/ingest.write |
| 성공 | 201 JSON; 200 동일 이벤트 중복 접수 확인 |

요청 필드

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| schemaVersion | string | 필수 | 문자열 1.0 고정. |
| eventId | string (uuid) | 필수 | 최초 이벤트 UUID. 재전송 중 유지. |
| eventCode | string | 필수 | 부록의 등록 이벤트 코드. 임의 코드는 422. |
| siteId | string | 필수 | 인증된 발신 엣지의 등록 센터와 일치. 1~128자 Identifier. |
| occurredAt | string (date-time) | 필수 | 실제 감지 시각. 시간대 필수, UTC Z 표기 권장. |
| incidentId | string (uuid) | 조건부 | DSC-102A/B·104A/B·106A/B·202A/B 필수. 같은 사건은 같은 값. |
| assets | AssetLink[] | 필수 | 등록 자산 1~100개. AssetLink 모델 참조. |
| metrics | object | 선택 | 이벤트 코드별 지표. 부록 수치 사전 참조. |
| confidence | number | 선택 | 유한 수치 0~1. |
| modelVersion | string | 선택 | 모델 버전 문자열 1~128자. 모델별 지표 범위 검사에 사용. |
| media | Media | 선택 | 선택 초기 클립. Media 모델 참조. 초기 revision은 서버에서 0. |

요청 본문 예제

```json
{
  "schemaVersion": "1.0",
  "eventId": "8a394e71-50ac-4cde-b691-48c171f3a062",
  "eventCode": "DSC-102A",
  "siteId": "incheon-01",
  "occurredAt": "2026-11-16T00:00:00.000Z",
  "incidentId": "a1ea9c1f-2c69-4e52-aabe-d7414986e31a",
  "assets": [
    {
      "assetId": "cctv-r01",
      "role": "detector"
    }
  ],
  "metrics": {
    "estimatedClearanceMarginM": 0.5
  },
  "media": {
    "clipId": "94028d0a-4ef7-4fd4-9695-d195a78b40ce",
    "status": "RECORDING"
  }
}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| eventId | UUID | 필수 | 접수 이벤트. |
| edgeServerId | string | 필수 | 인증으로 결정한 발신 엣지. |
| siteId | string | 필수 | 등록 센터. |
| receivedAt | date-time | 필수 | 최초 영속 저장 시각. |
| duplicate | boolean | 필수 | 201은 false, 같은 최초 본문의 재수신 200은 true. |

201 응답 예제

```json
{
  "eventId": "8a394e71-50ac-4cde-b691-48c171f3a062",
  "edgeServerId": "cctv-incheon-01",
  "siteId": "incheon-01",
  "receivedAt": "2026-11-16T00:00:00.000Z",
  "duplicate": false
}
```

동일한 최초 이벤트 재전송의 200 응답 예제

```json
{
  "eventId": "8a394e71-50ac-4cde-b691-48c171f3a062",
  "edgeServerId": "cctv-incheon-01",
  "siteId": "incheon-01",
  "receivedAt": "2026-11-16T00:00:00.000Z",
  "duplicate": true
}
```

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 403 | SITE_MISMATCH | 등록 엣지 없음, archive 역할 또는 siteId 불일치. |
| 403 | ASSET_MISMATCH | 미등록·다른 센터·발신 엣지에 미허용된 자산. |
| 409 | EVENT_ID_CONFLICT | 같은 eventId의 발신자 또는 최초 본문이 다름. |
| 422 | METRIC_RANGE | 등록된 모델·자산 수치 범위 밖. |
| 422 | METRIC_METADATA_REQUIRED | fireAnomalyScore·noiseVariance의 모델별 범위 미등록. |

처리 규칙

1. 이벤트·자산 연결·초기 클립 정보를 한 트랜잭션으로 저장한 뒤 201을 반환한다.

2. 같은 eventId·발신자·정규화한 최초 본문이면 200 duplicate=true이며 receivedAt을 유지한다. JSON 키 순서·공백은 비교에서 정규화한다.

3. 응답 유실 시 최초 ID와 본문을 재전송한다. 이후 media API 갱신을 최초 이벤트 본문에 덮어넣지 않는다.

4. edgeServerId·receivedAt·payloadHash·초기 media revision은 서버 소유 필드다. localMedia와 NAS 경로는 보내지 않는다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request POST "${API_BASE_URL}/ingest" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec01-request.json"
```

### 5 2 EC02 녹화 클립 상태 갱신

```http
PUT /ingest/events/{event_id}/media
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 해당 이벤트를 처음 전송한 엣지 |
| 필요 scope | dsc/ingest.write |
| 성공 | 200 JSON |

경로 변수 event_id는 필수 UUID다. 쿼리 파라미터는 없다.

요청 필드

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| clipId | string (uuid) | 필수 | 같은 클립의 불변 UUID. 최초 등록 후 교체 불가. |
| status | string | 필수 | RECORDING / AVAILABLE / MISSING / NOT_APPLICABLE. |
| startedAt | string (date-time) | 조건부 | AVAILABLE이면 필수. 클립 시작 시각. |
| endedAt | string (date-time) | 조건부 | AVAILABLE이면 필수. startedAt보다 뒤. 현장 보관 기간의 기준. |
| contentType | string | 조건부 | AVAILABLE 또는 업로드 등록에서 video/mp4. |
| sizeBytes | integer | 조건부 | 실제 파일 바이트 수. 양의 정수. 사본·업로드 등록 상한 536870912. |
| revision | integer | 필수 | MediaUpdate는 1 이상, 사본은 0 이상. 같은 번호의 내용은 불변. |

요청 본문 예제

```json
{
  "clipId": "94028d0a-4ef7-4fd4-9695-d195a78b40ce",
  "status": "AVAILABLE",
  "startedAt": "2026-11-16T00:00:00.000Z",
  "endedAt": "2026-11-16T00:00:01.000Z",
  "contentType": "video/mp4",
  "sizeBytes": 1048576,
  "revision": 1
}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| eventId | UUID | 필수 | 경로의 이벤트 ID. |
| clipId | UUID | 필수 | 불변 클립 ID. |
| revision | integer | 필수 | 접수한 revision. |
| status | enum | 필수 | 미디어 상태. |
| startedAt | date-time | 조건부 | 클립 시작 시각. AVAILABLE이면 포함. |
| endedAt | date-time | 조건부 | 클립 종료 시각. AVAILABLE이면 포함. |
| contentType | string | 조건부 | AVAILABLE이면 video/mp4. |
| sizeBytes | integer | 조건부 | AVAILABLE이면 실제 양의 바이트 수. |

200 응답 예제

```json
{
  "eventId": "8a394e71-50ac-4cde-b691-48c171f3a062",
  "clipId": "94028d0a-4ef7-4fd4-9695-d195a78b40ce",
  "status": "AVAILABLE",
  "startedAt": "2026-11-16T00:00:00.000Z",
  "endedAt": "2026-11-16T00:00:01.000Z",
  "contentType": "video/mp4",
  "sizeBytes": 1048576,
  "revision": 1
}
```

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 404 | NOT_FOUND | 이벤트 없음 또는 원래 발신 엣지가 아님. |
| 409 | CLIP_ID_CONFLICT | 최초 clipId 교체 시도. |
| 409 | MEDIA_REVISION_CONFLICT | 오래된 revision 또는 같은 revision의 다른 본문. |
| 409 | REPLICA_CONTENT_CONFLICT | 이미 확정된 사본과 AVAILABLE 크기가 다름. |

처리 규칙

1. 이벤트 저장 ACK 후 호출한다. revision은 1부터 증가하고 로컬에 영속 보관한다.

2. 현재 revision과 본문이 같으면 200으로 재확인한다. 최초 media가 없었다면 첫 갱신에서 한 번 등록할 수 있다.

3. 새 revision은 이전 revision에 고정된 진행 중 영상 작업을 실패 처리할 수 있다. 작업 담당자는 예전 시도로 완료를 통지하지 않는다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request PUT "${API_BASE_URL}/ingest/events/${EVENT_ID}/media" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec02-request.json"
```

### 5 3 EC03 엣지 및 워커 상태 보고

```http
POST /edge-server/heartbeat
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 등록된 field·archive·legacy 설치 |
| 필요 scope | dsc/edge-server.work |
| 성공 | 200 JSON |

요청 필드

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| schemaVersion | string | 필수 | 문자열 1.1 고정. |
| edgeServerVersion | string | 필수 | 실행 프로그램 버전, 1~128자. |
| observedAt | string (date-time) | 필수 | 엣지가 관측한 시각. 서버 수신 시각과 구분. |
| outboxPendingCount | integer | 필수 | 미전송 outbox 개수, 0 이상 정수. |
| diskFreeBytes | integer | 필수 | 관측한 여유 공간, 바이트 단위 0 이상 정수. |
| nasStatus | string | 필수 | OK / UNREACHABLE / READ_ERROR. |
| oldestPendingAt | string (date-time) | 조건부 | outboxPendingCount가 1 이상이면 필수. |
| replicaPendingCount | integer | 선택 | field의 미복제 사본 수, 0 이상 정수. |
| replicaQuarantinedCount | integer | 선택 | field의 격리 사본 수, 0 이상 정수. |

요청 본문 예제

```json
{
  "schemaVersion": "1.1",
  "edgeServerVersion": "1.2.0",
  "observedAt": "2026-11-16T00:00:00.000Z",
  "outboxPendingCount": 2,
  "oldestPendingAt": "2026-11-16T00:00:00.000Z",
  "diskFreeBytes": 10737418240,
  "nasStatus": "OK",
  "replicaPendingCount": 1,
  "replicaQuarantinedCount": 0
}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| edgeServerId | string | 필수 | 인증으로 결정한 설치 ID. |
| serverTime | date-time | 필수 | 서버에서 보고를 수신한 시각. |

200 응답 예제

```json
{
  "edgeServerId": "cctv-incheon-01",
  "serverTime": "2026-11-16T00:00:00.000Z"
}
```

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 403 | EDGE_SERVER_REQUIRED | 주체에 연결된 엣지 설치가 없음. |

처리 규칙

1. 기본 60초마다 보낸다. observedAt은 현장 관측 시각이며 serverTime과 구분한다.

2. 신규 작업 배정은 최근 180초 내 heartbeat와 nasStatus=OK를 요구한다. 관리 목록 OFFLINE 기준 5분과 구분한다.

3. field의 nasStatus는 현장 관리 사본 저장소 상태, archive는 NAS 볼륨 상태다. 복제 대기·격리 개수는 field가 추가 보고한다.

4. heartbeat 실패가 이벤트 outbox 전송과 현장 사본 보존을 중단시키지 않는다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request POST "${API_BASE_URL}/edge-server/heartbeat" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec03-request.json"
```

### 5 4 EC04 현장 검증 사본 등록

```http
POST /edge-server/replicas
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 이벤트 발신 field 엣지 |
| 필요 scope | dsc/edge-server.work |
| 성공 | 200 JSON |

요청 필드

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| sizeBytes | integer | 필수 | 실제 파일 바이트 수. 양의 정수. 사본·업로드 등록 상한 536870912. |
| checksumSha256 | string | 필수 | 전체 파일 SHA-256 32바이트의 표준 Base64. 16진수·ETag 불가. |
| eventId | string (uuid) | 필수 | 최초 이벤트 UUID. 재전송 중 유지. |
| clipId | string (uuid) | 필수 | 같은 클립의 불변 UUID. 최초 등록 후 교체 불가. |
| revision | integer | 필수 | MediaUpdate는 1 이상, 사본은 0 이상. 같은 번호의 내용은 불변. |

요청 본문 예제

```json
{
  "eventId": "8a394e71-50ac-4cde-b691-48c171f3a062",
  "clipId": "94028d0a-4ef7-4fd4-9695-d195a78b40ce",
  "revision": 1,
  "sizeBytes": 1048576,
  "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| eventId | UUID | 필수 | 대상 이벤트 UUID. |
| clipId | UUID | 필수 | 대상 불변 클립 UUID. |
| revision | integer | 필수 | 사본 미디어 revision. |
| edgeServerId | string | 필수 | 현장 사본 보유자. |
| sizeBytes | integer | 필수 | 파일 크기. |
| checksumSha256 | string | 필수 | 전체 파일 SHA-256 Base64. |
| status | string | 필수 | PRESENT 고정. |

200 응답 예제

```json
{
  "eventId": "8a394e71-50ac-4cde-b691-48c171f3a062",
  "clipId": "94028d0a-4ef7-4fd4-9695-d195a78b40ce",
  "revision": 1,
  "sizeBytes": 1048576,
  "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
  "edgeServerId": "cctv-incheon-01",
  "status": "PRESENT"
}
```

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 404 | NOT_FOUND | 원래 발신자가 아니거나 센터·모든 연결 자산 권한 부족. |
| 409 | REPLICA_METADATA_CONFLICT | 미디어 상태·clipId·revision·크기가 등록 정보와 다름. |
| 409 | REPLICA_CONTENT_CONFLICT | 같은 클립의 사본 크기·SHA-256 충돌. |
| 409 | REPLICA_RETIRED | 이미 retire한 현장 사본의 재등록 시도. |

처리 규칙

1. 로컬 관리 사본 확정 및 이벤트·해당 revision의 ACK 뒤에 호출한다. 초기 AVAILABLE 이벤트의 사본 revision은 0이다.

2. 백엔드는 source·센터·연결된 모든 자산 권한과 등록 media를 검증한다. 같은 내용 재등록은 같은 사본을 확인하고 PRESENT를 반환한다.

3. 같은 clipId의 파일 크기·해시는 revision이 바뀌어도 불변이다. PRESENT는 현장 보유 등록이며 NAS 저장 완료를 의미하지 않는다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request POST "${API_BASE_URL}/edge-server/replicas" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec04-request.json"
```

### 5 5 EC05 사무실 영상 전송 준비

```http
POST /edge-server/archive/prepare
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 이벤트 발신 field 엣지 |
| 필요 scope | dsc/edge-server.work |
| 성공 | 200 JSON |

요청 필드

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| sizeBytes | integer | 필수 | 실제 파일 바이트 수. 양의 정수. 사본·업로드 등록 상한 536870912. |
| checksumSha256 | string | 필수 | 전체 파일 SHA-256 32바이트의 표준 Base64. 16진수·ETag 불가. |
| eventId | string (uuid) | 필수 | 최초 이벤트 UUID. 재전송 중 유지. |
| clipId | string (uuid) | 필수 | 같은 클립의 불변 UUID. 최초 등록 후 교체 불가. |
| revision | integer | 필수 | MediaUpdate는 1 이상, 사본은 0 이상. 같은 번호의 내용은 불변. |

요청 본문 예제

```json
{
  "eventId": "8a394e71-50ac-4cde-b691-48c171f3a062",
  "clipId": "94028d0a-4ef7-4fd4-9695-d195a78b40ce",
  "revision": 1,
  "sizeBytes": 1048576,
  "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| transferId | UUID | 필수 | 이번 전송 권한의 식별자. |
| receiverId | string | 필수 | 등록된 사무실 수신 설치. |
| transferToken | string | 필수 | 전송 한 건에 한정한 비밀 토큰. |
| uploadUrl | URI | 필수 | 사무실 HTTPS 수신기 URL. S3 URL과 구분. |
| expiresAt | date-time | 필수 | 전송 권한 만료 시각. |
| createdAt | date-time | 필수 | 클립 endedAt에 따른 보관 기준 시각. prepare 호출 시각이 아님. |

200 응답 예제

```json
{
  "transferId": "0827d6f8-9f75-487a-90b9-75943d55b84f",
  "receiverId": "office-incheon-01",
  "transferToken": "example-transfer-token-for-documentation-only-0001",
  "uploadUrl": "https://archive.example.invalid/archive/transfers/0827d6f8-9f75-487a-90b9-75943d55b84f",
  "expiresAt": "2026-11-16T00:15:00.000Z",
  "createdAt": "2026-11-16T00:00:01.000Z"
}
```

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 404 | NOT_FOUND | 발신·자산 권한 부족 또는 이벤트 없음. |
| 409 | REPLICA_METADATA_CONFLICT | 미디어 상태·clipId·revision·크기가 등록 정보와 다름. |
| 409 | REPLICA_CONTENT_CONFLICT | 같은 클립의 사본 크기·SHA-256 충돌. |
| 409 | REPLICA_RETIRED | 이미 retire한 현장 사본의 재등록 시도. |
| 409 | ARCHIVE_NOT_CONFIGURED | 허용된 archive 설치·수신 주소·권한 등록이 필요. |

처리 규칙

1. 기본 유효기간 15분. transferId·발신·수신·이벤트·클립·revision·크기·해시에 결합한다.

2. prepare를 반복하면 새 transferId와 토큰을 발급한다. eventId 중복과 같은 멱등 응답을 가정하지 않는다.

3. uploadUrl의 HTTPS·등록 호스트 allowlist·transferId를 검증하고 수신기에 직접 연결한다. 클립 바이트를 이 AWS API 본문에 보내지 않는다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request POST "${API_BASE_URL}/edge-server/archive/prepare" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec05-request.json"
```

### 5 6 EC06 수신 전송 권한 확인

```http
POST /edge-server/archive/{transfer_id}/authorize
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 해당 transferId에 지정된 사무실 수신기 |
| 필요 scope | dsc/edge-server.work |
| 성공 | 200 JSON |

경로 변수 transfer_id는 필수 UUID다. 쿼리 파라미터는 없다.

요청 필드

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| transferToken | string | 필수 | prepare에서 받은 전송 토큰, 32~256자. AWS API에서는 JSON 본문에 전달. |

요청 본문 예제

```json
{
  "transferToken": "example-transfer-token-for-documentation-only-0001"
}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| transferId | UUID | 필수 | 권한이 묶인 전송·이벤트·클립. |
| eventId | UUID | 필수 | 대상 이벤트 UUID. |
| clipId | UUID | 필수 | 대상 불변 클립 UUID. |
| revision | integer | 필수 | 고정된 미디어 revision. |
| sourceId | string | 필수 | 이벤트 발신 field 설치 ID. |
| receiverId | string | 필수 | 지정된 사무실 archive 설치 ID. |
| sizeBytes | integer | 필수 | 실제 파일 바이트 수. 신규 배정이면 고정된 검증값. |
| checksumSha256 | string | 필수 | 전체 파일 SHA-256 32바이트의 표준 Base64. |
| createdAt | date-time | 필수 | 클립 생성·보관 기준. |
| status | enum | 필수 | 완료 전 PENDING, 이미 완료된 유효 전송이면 STORED. |

200 응답 예제

```json
{
  "createdAt": "2026-11-16T00:00:01.000Z",
  "transferId": "0827d6f8-9f75-487a-90b9-75943d55b84f",
  "eventId": "8a394e71-50ac-4cde-b691-48c171f3a062",
  "clipId": "94028d0a-4ef7-4fd4-9695-d195a78b40ce",
  "revision": 1,
  "sourceId": "cctv-incheon-01",
  "receiverId": "office-incheon-01",
  "sizeBytes": 1048576,
  "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
  "status": "PENDING"
}
```

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 404 | NOT_FOUND | 전송 없음, 수신자·토큰 불일치 또는 발신/수신 권한 회수. |
| 409 | TRANSFER_EXPIRED | 전송 권한의 유효기간 만료. 새 prepare 필요. |

처리 규칙

1. AWS 요청 Authorization은 수신기 자체 Cognito access token이다. field가 준 transferToken은 JSON 본문에 넣는다.

2. 원래 source와 지정 receiver의 활성 상태·관계·센터·모든 연결 자산 권한을 다시 확인한다.

3. authorize 성공 자체는 NAS 파일 검증 완료가 아니다. 수신기는 반환된 메타데이터로 실제 파일·바이트를 확인한다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request POST "${API_BASE_URL}/edge-server/archive/${TRANSFER_ID}/authorize" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec06-request.json"
```

### 5 7 EC07 NAS 저장 완료 보고

```http
POST /edge-server/archive/{transfer_id}/complete
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 해당 transferId에 지정된 사무실 수신기 |
| 필요 scope | dsc/edge-server.work |
| 성공 | 200 JSON |

경로 변수 transfer_id는 필수 UUID다. 쿼리 파라미터는 없다.

요청 필드

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| sizeBytes | integer | 필수 | 실제 파일 바이트 수. 양의 정수. 사본·업로드 등록 상한 536870912. |
| checksumSha256 | string | 필수 | 전체 파일 SHA-256 32바이트의 표준 Base64. 16진수·ETag 불가. |
| transferToken | string | 필수 | prepare에서 받은 전송 토큰, 32~256자. AWS API에서는 JSON 본문에 전달. |

요청 본문 예제

```json
{
  "transferToken": "example-transfer-token-for-documentation-only-0001",
  "sizeBytes": 1048576,
  "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| transferId | UUID | 필수 | 전송 및 저장된 콘텐츠 식별자. |
| eventId | UUID | 필수 | 대상 이벤트 UUID. |
| clipId | UUID | 필수 | 대상 불변 클립 UUID. |
| revision | integer | 필수 | 고정된 미디어 revision. |
| sourceId | string | 필수 | 이벤트 발신 field 설치 ID. |
| receiverId | string | 필수 | 지정된 사무실 archive 설치 ID. |
| sizeBytes | integer | 필수 | 실제 파일 바이트 수. 신규 배정이면 고정된 검증값. |
| checksumSha256 | string | 필수 | 전체 파일 SHA-256 32바이트의 표준 Base64. |
| createdAt | date-time | 필수 | 클립 보관 기준 시각. |
| status | string | 필수 | STORED 고정. |

200 응답 예제

```json
{
  "createdAt": "2026-11-16T00:00:01.000Z",
  "transferId": "0827d6f8-9f75-487a-90b9-75943d55b84f",
  "eventId": "8a394e71-50ac-4cde-b691-48c171f3a062",
  "clipId": "94028d0a-4ef7-4fd4-9695-d195a78b40ce",
  "revision": 1,
  "sourceId": "cctv-incheon-01",
  "receiverId": "office-incheon-01",
  "sizeBytes": 1048576,
  "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
  "status": "STORED"
}
```

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 404 | NOT_FOUND | 전송 없음, 수신자·토큰 불일치 또는 발신/수신 권한 회수. |
| 409 | TRANSFER_EXPIRED | 전송 권한의 유효기간 만료. 새 prepare 필요. |
| 409 | ARCHIVE_VERIFICATION_FAILED | 전송에 고정한 크기 또는 해시와 보고값 불일치. |
| 409 | REPLICA_CONTENT_CONFLICT | 현장 사본 없음 또는 사무실 사본 내용 충돌. |

처리 규칙

1. 수신기는 임시 파일 쓰기·실제 크기/해시 검사·파일 및 디렉터리 fsync·불변 확정·로컬 manifest 기록 후에만 호출한다.

2. 백엔드는 사무실 사본 등록과 전송 완료를 기록한다. 현장에는 이 STORED receipt까지 검증한 뒤 ACK한다.

3. 같은 완료 내용의 재보고는 STORED를 반환한다. 이미 완료한 전송의 complete는 토큰 만료 후에도 현재 권한·내용이 유효하면 조정 가능하다.

4. authorize는 완료된 전송이어도 만료되면 거부한다. 현장 ACK 유실 복구는 새 prepare와 수신기 verify를 사용한다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request POST "${API_BASE_URL}/edge-server/archive/${TRANSFER_ID}/complete" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec07-request.json"
```

### 5 8 EC08 현장 사본 정리 승인

```http
POST /edge-server/replicas/retire
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 이벤트 발신 field 엣지 |
| 필요 scope | dsc/edge-server.work |
| 성공 | 200 JSON |

요청 필드

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| sizeBytes | integer | 필수 | 실제 파일 바이트 수. 양의 정수. 사본·업로드 등록 상한 536870912. |
| checksumSha256 | string | 필수 | 전체 파일 SHA-256 32바이트의 표준 Base64. 16진수·ETag 불가. |
| eventId | string (uuid) | 필수 | 최초 이벤트 UUID. 재전송 중 유지. |
| clipId | string (uuid) | 필수 | 같은 클립의 불변 UUID. 최초 등록 후 교체 불가. |
| revision | integer | 필수 | MediaUpdate는 1 이상, 사본은 0 이상. 같은 번호의 내용은 불변. |

요청 본문 예제

```json
{
  "eventId": "8a394e71-50ac-4cde-b691-48c171f3a062",
  "clipId": "94028d0a-4ef7-4fd4-9695-d195a78b40ce",
  "revision": 1,
  "sizeBytes": 1048576,
  "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| eventId | UUID | 필수 | 대상 이벤트 UUID. |
| clipId | UUID | 필수 | 대상 불변 클립 UUID. |
| revision | integer | 필수 | 정리 대상 revision. |
| sizeBytes | integer | 필수 | 실제 파일 바이트 수. 신규 배정이면 고정된 검증값. |
| checksumSha256 | string | 필수 | 전체 파일 SHA-256 32바이트의 표준 Base64. |
| status | string | 필수 | RETIRED. 물리 파일 삭제 완료 응답은 아님. |

200 응답 예제

```json
{
  "eventId": "8a394e71-50ac-4cde-b691-48c171f3a062",
  "clipId": "94028d0a-4ef7-4fd4-9695-d195a78b40ce",
  "revision": 1,
  "sizeBytes": 1048576,
  "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
  "status": "RETIRED"
}
```

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 404 | NOT_FOUND | 발신·권한·대상 사본 식별 정보 불일치. |
| 409 | REPLICA_IN_USE | 보관 기간·NAS 사본·건강 상태·진행 작업 보호 조건 미충족. |

처리 규칙

1. 기본 endedAt+30일이 지나고 새 전송 권한으로 NAS 실제 크기·해시를 다시 확인한 뒤 호출한다.

2. 백엔드는 동일 NAS 사본, 건강한 archive, QUEUED·UPLOADING 작업 부재를 확인하고 신규 현장 배정에서 제외한다.

3. RETIRED 응답을 확인한 현장만 파일 잠금 아래에서 같은 바이트의 관리 사본을 삭제하고 DELETED를 기록한다.

4. 동일 retire 재요청은 RETIRED를 재확인한다. 응답 유실·삭제 중 재시작 시 RETIRING 기록으로 복구한다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request POST "${API_BASE_URL}/edge-server/replicas/retire" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec08-request.json"
```

### 5 9 EC09 업로드 작업 받기

```http
POST /edge-server/jobs/claim
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 사무실 주 워커 또는 현장 예비 업로더 |
| 필요 scope | dsc/edge-server.work |
| 성공 | 200 JSON; 204 배정 가능한 작업 없음. 응답 본문 없음 |

요청 필드

빈 JSON 객체 `{}`를 전송한다. 본문 생략과 추가 필드는 허용하지 않는다.

요청 본문 예제

```json
{}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| requestId | UUID | 필수 | 영상 업로드 작업 UUID. |
| eventId | UUID | 필수 | 대상 이벤트 UUID. |
| clipId | UUID | 필수 | 대상 불변 클립 UUID. |
| attempt | integer | 필수 | 현재 시도 번호. 새 claim 성공 때 증가. |
| leaseToken | string | 필수 | 이번 시도 전용 비밀 토큰. |
| leaseExpiresAt | date-time | 필수 | 현재 작업 점유 만료 시각. |
| revision | integer | 조건부 | 신규 field·archive 배정에 포함. legacy는 생략 가능. |
| sizeBytes | integer | 조건부 | 실제 파일 바이트 수. 신규 배정이면 고정된 검증값. |
| checksumSha256 | string | 조건부 | 전체 파일 SHA-256 32바이트의 표준 Base64. |

200 응답 예제

```json
{
  "requestId": "fa19c81b-6590-4c74-89d7-9938b52b0817",
  "eventId": "8a394e71-50ac-4cde-b691-48c171f3a062",
  "clipId": "94028d0a-4ef7-4fd4-9695-d195a78b40ce",
  "attempt": 1,
  "leaseToken": "example-lease-token-for-documentation-only-0001",
  "leaseExpiresAt": "2026-11-16T00:05:00.000Z",
  "revision": 1,
  "sizeBytes": 1048576,
  "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
}
```

204 응답은 본문이 없다. Content-Length가 0인 응답에 대해 JSON 파싱을 시도하지 않는다.

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 403 | EDGE_SERVER_REQUIRED | 등록된 설치가 아님. |
| 404 | NOT_FOUND | 배정 후 업로더 권한이 회수됨. |

처리 규칙

1. 기본 5초마다 호출한다. 작업이 없으면 204이며 JSON을 읽지 않는다. 204는 업무 오류가 아니다.

2. SQS는 백엔드가 소비한다. 엣지·워커에 SQS용 AWS 자격 증명을 배포하지 않는다.

3. 사무실을 우선하고 검증된 사본·건강 상태·요청자 현재 권한에 따라 배정한다. 유효 lease는 한 개다.

4. 응답 유실 후 같은 lease를 다시 돌려주는 계약은 없다. 기존 lease가 만료되기 전에는 다른 시도가 이를 덮어쓸 수 없다.

5. 기본 lease 5분, 작업 전체 기한 30분, 최대 3회 시도. 이전 시도의 토큰·객체를 새 작업에 재사용하지 않는다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request POST "${API_BASE_URL}/edge-server/jobs/claim" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec09-request.json"
```

### 5 10 EC10 업로드 작업 점유 연장

```http
POST /edge-server/jobs/{request_id}/lease
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 현재 유효 lease를 가진 업로더 |
| 필요 scope | dsc/edge-server.work |
| 성공 | 200 JSON |

경로 변수 request_id는 필수 UUID다. 쿼리 파라미터는 없다.

요청 필드

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| leaseToken | string | 필수 | claim에서 받은 현재 시도의 lease 토큰, 20~256자. |

요청 본문 예제

```json
{
  "leaseToken": "example-lease-token-for-documentation-only-0001"
}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| leaseExpiresAt | date-time | 필수 | 연장된 만료 시각. 작업 전체 기한을 넘지 않음. |

200 응답 예제

```json
{
  "leaseExpiresAt": "2026-11-16T00:06:00.000Z"
}
```

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 404 | NOT_FOUND | 작업 없음, 현재 담당자가 아님 또는 업로더 권한 회수. |
| 409 | LEASE_CONFLICT | 현재 시도의 토큰과 일치하지 않음. |
| 409 | LEASE_EXPIRED | 업로드 상태가 아니거나 lease·작업 기한 만료. |
| 409 | REQUESTER_DISABLED | 요청자의 읽기 권한 회수. |

처리 규칙

1. 기본 60초마다 연장한다. 서버는 SQS 가시성 시간도 함께 조정한다.

2. 업로드·검증 중에도 갱신한다. 연장이 거부되면 현재 시도의 소유권을 전제로 한 완료 처리를 중단한다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request POST "${API_BASE_URL}/edge-server/jobs/${REQUEST_ID}/lease" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec10-request.json"
```

### 5 11 EC11 S3 업로드 URL 발급

```http
POST /edge-server/jobs/{request_id}/upload-url
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 현재 유효 lease를 가진 업로더 |
| 필요 scope | dsc/edge-server.work |
| 성공 | 200 JSON |

경로 변수 request_id는 필수 UUID다. 쿼리 파라미터는 없다.

요청 필드

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| sizeBytes | integer | 필수 | 실제 파일 바이트 수. 양의 정수. 사본·업로드 등록 상한 536870912. |
| checksumSha256 | string | 필수 | 전체 파일 SHA-256 32바이트의 표준 Base64. 16진수·ETag 불가. |
| leaseToken | string | 필수 | claim에서 받은 현재 시도의 lease 토큰, 20~256자. |
| contentType | string | 필수 | AVAILABLE 또는 업로드 등록에서 video/mp4. |

요청 본문 예제

```json
{
  "leaseToken": "example-lease-token-for-documentation-only-0001",
  "sizeBytes": 1048576,
  "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
  "contentType": "video/mp4"
}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| uploadUrl | URI | 필수 | 이번 시도 객체의 서명 PUT URL. |
| urlExpiresAt | date-time | 필수 | URL 만료. 현재 설정과 작업 잔여 기한으로 제한. |
| headers | object | 필수 | PUT에 그대로 적용할 서명 헤더. |
| headers.Content-Type | string | 필수 | video/mp4. |
| headers.x-amz-checksum-sha256 | string | 필수 | 요청의 SHA-256 Base64. |

200 응답 예제

```json
{
  "uploadUrl": "https://s3.example.invalid/clip.mp4?signature=EXAMPLE",
  "urlExpiresAt": "2026-11-16T00:15:00.000Z",
  "headers": {
    "Content-Type": "video/mp4",
    "x-amz-checksum-sha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
  }
}
```

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 404 | NOT_FOUND | 작업 없음, 담당자 불일치 또는 권한 회수. |
| 404 | CLIP_UNAVAILABLE | 현재 미디어가 AVAILABLE이 아님. |
| 409 | LEASE_CONFLICT | 현재 시도의 토큰과 일치하지 않음. |
| 409 | LEASE_EXPIRED | 업로드 상태가 아니거나 lease·작업 기한 만료. |
| 409 | REQUESTER_DISABLED | 요청자의 읽기 권한 회수. |
| 409 | MEDIA_REVISION_CONFLICT | 배정된 미디어 revision 변경. |
| 409 | UPLOAD_VERIFICATION_FAILED | 검증된 사본의 해시와 요청값 불일치. |
| 409 | UPLOAD_SIZE_MISMATCH | 등록된 파일 크기와 불일치. |
| 409 | UPLOAD_METADATA_CONFLICT | 같은 시도에서 이미 등록한 크기·해시 변경. |

처리 규칙

1. 실제 파일 전체의 크기와 SHA-256을 계산해 claim의 고정값과 먼저 대조한다.

2. 기본 URL 유효기간은 15분이며 작업 전체 잔여 기한으로 줄어들 수 있다. lease는 별도로 연장해야 한다.

3. 같은 시도의 동일 메타데이터 재요청은 같은 객체 키에 대한 새 서명 URL을 받을 수 있다. 다른 내용으로 변경하지 않는다.

4. URL의 원문·headers 값을 보존하고 Content-Length를 실제 바이트 수로 보낸다. 임의 버킷·객체 키·리다이렉트를 사용하지 않는다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request POST "${API_BASE_URL}/edge-server/jobs/${REQUEST_ID}/upload-url" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec11-request.json"
```

### 5 12 EC12 S3 업로드 완료 통지

```http
POST /edge-server/jobs/{request_id}/complete
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 현재 유효 lease를 가진 업로더 |
| 필요 scope | dsc/edge-server.work |
| 성공 | 200 JSON |

경로 변수 request_id는 필수 UUID다. 쿼리 파라미터는 없다.

요청 필드

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| leaseToken | string | 필수 | claim에서 받은 현재 시도의 lease 토큰, 20~256자. |

요청 본문 예제

```json
{
  "leaseToken": "example-lease-token-for-documentation-only-0001"
}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| status | string | 필수 | 검증 성공 후 READY. |

200 응답 예제

```json
{
  "status": "READY"
}
```

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 404 | NOT_FOUND | 작업 없음, 현재 담당자 불일치 또는 업로더 권한 회수. |
| 409 | LEASE_CONFLICT | 현재 시도의 토큰과 일치하지 않음. |
| 409 | LEASE_EXPIRED | 업로드 상태가 아니거나 lease·작업 기한 만료. |
| 409 | REQUESTER_DISABLED | 요청자의 읽기 권한 회수. |
| 409 | UPLOAD_NOT_REGISTERED | upload-url 조건 등록을 먼저 해야 함. |
| 409 | UPLOAD_VERIFICATION_FAILED | S3 실제 크기·SHA-256·Content-Type 불일치. |

처리 규칙

1. S3 PUT의 성공만으로 READY가 되지 않는다. 백엔드가 HEAD로 실제 객체를 검증하고 시각을 다시 검사한 후 READY를 저장한다.

2. ETag를 SHA-256으로 취급하지 않는다. 현재 lease·시도·객체에만 완료를 적용한다.

3. 동일한 완료 토큰의 재통지는 READY를 반환한다. 다른 엣지로 배정됐으면 404, 같은 담당자의 다른 시도 토큰이면 409가 될 수 있다.

4. READY 이후 기본 24시간 동안 고객에게 임시 다운로드를 제공한다. 업로더에게 이 응답에서 다운로드 URL을 반환하지 않는다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request POST "${API_BASE_URL}/edge-server/jobs/${REQUEST_ID}/complete" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec12-request.json"
```

### 5 13 EC13 업로드 실패 통지

```http
POST /edge-server/jobs/{request_id}/fail
```

| 항목 | 내용 |
|---|---|
| 서버 | API_BASE_URL |
| 호출 주체 | 현재 유효 lease를 가진 업로더 |
| 필요 scope | dsc/edge-server.work |
| 성공 | 200 JSON |

경로 변수 request_id는 필수 UUID다. 쿼리 파라미터는 없다.

요청 필드

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| leaseToken | string | 필수 | claim에서 받은 현재 시도의 lease 토큰, 20~256자. |
| failureCode | string | 필수 | SOURCE_NOT_FOUND / SOURCE_UNREACHABLE / UPLOAD_FAILED / UPLOAD_VERIFICATION_FAILED. |
| retryable | boolean | 필수 | boolean 필수. 서버가 역할·기한·횟수에 따라 최종 재시도 여부를 결정. |

요청 본문 예제

```json
{
  "leaseToken": "example-lease-token-for-documentation-only-0001",
  "failureCode": "SOURCE_UNREACHABLE",
  "retryable": true
}
```

응답 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| status | enum | 필수 | 재시도 가능하면 QUEUED, 종결이면 FAILED. |

200 응답 예제

```json
{
  "status": "QUEUED"
}
```

추가 업무 오류

| HTTP | error.code | 발생 조건 |
|---|---|---|
| 404 | NOT_FOUND | 작업 없음, 담당자 불일치 또는 권한 회수. |
| 409 | LEASE_CONFLICT | 현재 시도의 토큰과 일치하지 않음. |
| 409 | LEASE_EXPIRED | 업로드 상태가 아니거나 lease·작업 기한 만료. |
| 409 | REQUESTER_DISABLED | 요청자의 읽기 권한 회수. |

처리 규칙

1. 서버가 역할·남은 기한·시도 횟수로 최종 재시도 여부를 결정한다. 요청 retryable을 그대로 최종 판정으로 신뢰하지 않는다.

2. field·archive는 다른 검증 사본으로 제한된 재시도가 가능하다. legacy의 SOURCE_NOT_FOUND는 종결 실패다.

3. 같은 실패 요청이 이미 처리돼 작업이 QUEUED·FAILED로 바뀌었다면 재통지가 409가 될 수 있다. 항상 멱등 성공을 기대하지 않는다.

cURL 호출 예제

```sh
curl -sS --fail-with-body --connect-timeout 5 --max-time 30 \
  --request POST "${API_BASE_URL}/edge-server/jobs/${REQUEST_ID}/fail" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/edge-cloud-api/ec13-request.json"
```

## 6 연관 영상 전송 인터페이스

이 장의 서버는 AWS JSON API와 다르다. 사무실 수신기에는 전송 토큰을, S3에는 객체별 서명 조건을 사용한다. 영상 바이트가 API Gateway나 Lambda를 경유하지 않는다.

### 6 1 사무실 기존 사본 확인

```http
POST /archive/transfers/{transfer_id}/verify
```

서버는 EC05의 uploadUrl 뒤에 /verify를 붙인 주소다. Authorization: Bearer <transferToken>을 전송한다. 요청 본문·쿼리 파라미터는 없으며 필수 경로 변수는 transfer_id UUID다. 자체 Cognito access token을 이 헤더에 넣지 않는다.

200이면 수신기가 실제 파일의 크기·SHA-256·볼륨을 확인하고 AWS authorize·complete를 거쳐 STORED receipt를 반환한다. receipt 필드는 EC07과 같다. 404는 파일 또는 볼륨이 없을 수 있음을 나타내며 field는 PUT 전송을 시도한다. PUT에서도 볼륨·파일 검증이 다시 적용된다.

수신기 오류는 JSON detail 형식이다. 401 capability 누락·형식 오류, 404 파일·볼륨 없음 또는 백엔드 미공개, 409 내용·메타데이터·전송 상태 불일치, 503 접근·시간 초과·백엔드 일시 실패를 처리한다. 경로 UUID가 잘못되면 FastAPI 기본 422 검증 응답이다. AWS 공통 error 래퍼와 혼동하지 않는다.

```json
{
  "detail": "archive metadata or checksum mismatch"
}
```

### 6 2 사무실 클립 저장

```http
PUT /archive/transfers/{transfer_id}
```

| 항목 | 규격 |
|---|---|
| 서버 | EC05 uploadUrl의 사무실 수신기. |
| Authorization | Bearer <transferToken> |
| Content-Type | video/mp4 |
| Content-Length | 전송 권한에 고정된 실제 sizeBytes의 10진 문자열. |
| 본문 | 완성 MP4의 원본 바이너리. multipart/form-data·JSON·Base64 래퍼 아님. |
| 성공 | 200 STORED receipt. EC07과 같은 필드. |
| 오류 | verify와 같은 detail 형식. 잘못된 크기·해시·경로·볼륨을 성공 처리하지 않음. |

수신기는 파일 확정·디렉터리 동기화·manifest·AWS 완료 보고까지 확인한다. ACK 유실 시 새 EC05와 verify로 기존 파일을 다시 읽고 조정한다. 수신기에는 같은 대상 경로의 다른 바이트를 덮어쓰지 않는다.

```sh
curl --silent --show-error --fail-with-body \
  --connect-timeout 5 --max-time 120 \
  --request PUT "${ARCHIVE_UPLOAD_URL}" \
  --header "Authorization: Bearer ${TRANSFER_TOKEN}" \
  --header "Content-Type: video/mp4" \
  --header "Content-Length: ${SIZE_BYTES}" \
  --data-binary "@${CLIP_FILE}"
```

### 6 3 S3 직접 업로드

EC11이 반환한 uploadUrl 원문에 PUT한다. JSON이 아닌 완성 MP4 바이너리를 보내고 headers의 Content-Type·x-amz-checksum-sha256, 실제 Content-Length를 그대로 적용한다. Authorization Bearer를 추가하지 않는다. 서명 URL 전체를 로그에 출력하지 않고 HTTP 리다이렉트도 따르지 않는다.

S3 PUT의 2xx만 확인한 뒤 EC12 complete를 호출한다. S3의 오류는 XML 등 서비스 자체 형식이다. URL 만료·서명 불일치·네트워크 실패를 AWS JSON 오류와 구분한다. 현재 lease가 유효할 때 같은 시도의 EC11로 URL을 다시 받을 수 있고, lease를 잃었으면 예전 시도 완료를 중단한다.

```sh
curl --silent --show-error --fail-with-body \
  --connect-timeout 5 --max-time 120 \
  --request PUT "${S3_UPLOAD_URL}" \
  --header "Content-Type: video/mp4" \
  --header "x-amz-checksum-sha256: ${CHECKSUM_SHA256}" \
  --header "Content-Length: ${SIZE_BYTES}" \
  --data-binary "@${CLIP_FILE}"
```

## 7 호출 순서와 장애 복구

### 7 1 이벤트와 사무실 복제

1. field가 로컬 완성 입력·관리 사본·outbox를 영속 접수한다.

2. EC01 이벤트를 전송하고 ACK를 확인한다. RECORDING으로 보냈으면 EC02 AVAILABLE을 전송하고 해당 revision ACK를 확인한다.

3. EC04로 현장 사본을 등록하고 EC05로 사무실 전송 권한을 받는다.

4. 반환된 HTTPS 호스트·receiver·transferId를 검사한 뒤 사무실 verify를 호출한다.

5. 기존 파일이 없으면 사무실 PUT으로 바이트를 보낸다. 수신기는 EC06 authorize, 파일·manifest 확정, EC07 complete를 수행한다.

6. field는 STORED receipt의 sourceId·receiverId·eventId·clipId·revision·크기·해시를 모두 대조하고 로컬 상태를 기록한다.

### 7 2 요청된 클립 업로드

1. 고객이 기존 클립 요청 API를 호출하면 서버가 요청자 권한·클립 정보를 확인하고 작업을 만든다. 단순 상태 조회는 새 작업을 만들지 않는다.

2. 건강한 사무실 워커를 우선 배정한다. 워커는 EC03 heartbeat와 EC09 claim을 반복한다.

3. 200 claim의 requestId·attempt·leaseToken·미디어 식별자·크기·해시를 확인한다. 204이면 다음 주기까지 기다린다.

4. 실제 파일을 검증하고 EC11로 PUT URL을 받는다. 진행 중에는 기본 60초마다 EC10으로 lease를 연장한다.

5. S3에 직접 PUT한 뒤 EC12를 호출한다. 백엔드 HEAD 검증 후 READY가 되며, 고객은 별도 상태 API에서 짧은 GET URL을 받는다.

6. 소스 접근·업로드가 실패하면 유효 lease로 EC13을 보고한다. 사무실 장애 시 검증된 현장 사본과 독립 현장→AWS 회선으로 재배정한다.

### 7 3 복구와 중복 요청 처리표

| 상황 | 동작 | 보존할 정보 |
|---|---|---|
| EC01 ACK 유실 | 같은 최초 본문 재전송. 200 duplicate 확인. | eventId·최초 본문·outbox |
| EC02 ACK 유실 | 같은 revision·본문 재전송. | clipId·revision·본문 |
| 사무실 PUT ACK 유실 | 새 EC05 → verify → STORED 조정. | 현장 관리 사본·동일 크기·해시 |
| 전송 권한 만료 | 새 EC05 발급. 오래된 transferToken 반복 금지. | 불변 콘텐츠 정보 |
| claim ACK 유실 | 새 claim으로 예전 lease 회수를 보장하지 않음. 만료·재배정 프로토콜 사용. | requestId를 받았다면 시도 정보도 보존 |
| 업로드 중 lease 만료 | 이전 시도 중단. 새 담당자·새 객체만 완료 가능. | 불변 클립 사본 |
| 완료 응답 유실 | 현재와 동일한 완료 토큰으로 EC12 재확인. | requestId·attempt·토큰 |
| retire ACK 유실 | 같은 EC08로 RETIRED 확인 후 잠금·동일 해시 확인·삭제. | RETIRING 기록 |
| 현장·사무실 동시 장애 | 가능한 독립 사본·회선 복구까지 대기. | 미전송 outbox·미확인 사본 |

### 7 4 현장 사본의 보관과 삭제

현장 관리 사본은 기본 endedAt 기준 30일 이후에도 NAS 재검증·진행 작업 보호·EC08 승인을 모두 만족할 때만 삭제한다. 미전송·미확인·격리·작업 중 파일은 보관한다. 기간만으로 디스크 용량 상한을 보장하지 않으므로 누적량과 여유 공간을 관측한다. NAS의 장기 보관·백업과 양쪽 동시 장애·보관 기간 밖 클립의 가용성은 운영 정책과 현장 검수 항목이다.

## 8 데이터 사전과 기본값

### 8 1 자산과 초기 클립 모델

siteId·assetId의 Identifier는 1~128자이며 `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}` 패턴을 따른다. 영문자·숫자로 시작하고 이후 밑줄·점·콜론·하이픈을 포함할 수 있다. 등록 자산·센터 허용 검사도 별도로 적용한다.

AssetLink

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| assetId | string | 필수 | 해당 센터에 등록되고 발신 엣지에 허용된 자산 식별자. |
| role | string | 필수 | detector / subject / context. |

Media

| 필드 | 타입 | 필수 | 설명 및 제약 |
|---|---|---|---|
| clipId | string (uuid) | 필수 | 같은 클립의 불변 UUID. 최초 등록 후 교체 불가. |
| status | string | 필수 | RECORDING / AVAILABLE / MISSING / NOT_APPLICABLE. |
| startedAt | string (date-time) | 조건부 | AVAILABLE이면 필수. 클립 시작 시각. |
| endedAt | string (date-time) | 조건부 | AVAILABLE이면 필수. startedAt보다 뒤. 현장 보관 기간의 기준. |
| contentType | string | 조건부 | AVAILABLE 또는 업로드 등록에서 video/mp4. |
| sizeBytes | integer | 조건부 | 실제 파일 바이트 수. 양의 정수. 사본·업로드 등록 상한 536870912. |

AVAILABLE이면 startedAt·endedAt·contentType·sizeBytes가 모두 필요하고 endedAt>startedAt이어야 한다. 초기 Media에는 revision을 보내지 않는다. MediaUpdate에만 revision을 포함한다. RECORDING·MISSING·NOT_APPLICABLE은 AVAILABLE의 추가 필드를 필수로 요구하지 않는다.

### 8 2 이벤트 코드

| 코드 | 의미 | 사건 연결 |
|---|---|---|
| DSC-101 | 카메라 연결 오류 | 단건 |
| DSC-102A / DSC-102B | 통로 물체 감지 / 제거 | 같은 incidentId |
| DSC-103A / DSC-103B / DSC-103C | 배터리 입고 / 이동 / 출고 | 개별 기록 |
| DSC-104A / DSC-104B | 랙 이물질 감지 / 제거 | 같은 incidentId |
| DSC-105 | 운송 중 배터리 낙하 | 단건 |
| DSC-106A / DSC-106B | AMR 이동 정지 / 재개 | 같은 incidentId |
| DSC-201 | 온도 센서 연결 오류 | 단건 |
| DSC-202A / DSC-202B | 온도 이상 / 정상화 | 같은 incidentId |
| DSC-203 | 온도 센서 비정상 데이터 | 단건 |
| DSC-301 | 영상 분석 파이프라인 오류 | 단건 |
| DSC-302 | 정상 배경 갱신 필요 | 단건 |
| DSC-303 | 영상 저장 오류 | 단건 |

발생·해소는 서로 다른 eventId다. 사건 재발 시 새 incidentId를 만들며 재시작·순서 역전에도 원본 시각과 사건 연결을 유지한다. 서버가 온도 이상만으로 화재를 확정하거나 식별하지 못한 배터리의 상태를 임의로 생성하지 않는다.

### 8 3 metrics 수치 사전

| 필드 | 허용 코드 | 단위와 범위 |
|---|---|---|
| pingLatencyMs | DSC-101 | ms, 0 이상 |
| packetLossRatePct | DSC-101 | %, 0~100 |
| estimatedClearanceMarginM | DSC-102A / B | m, 0 이상 |
| delaySec | 모든 등록 코드 | 초, 0 이상 |
| estimatedDropHeightM | DSC-105 | m, 0 이상 |
| readFailureCount | DSC-201 | 횟수, 0 이상 정수 값의 number |
| fireAnomalyScore | DSC-202A | 유한 수치, 자산의 modelVersion별 등록 범위 필요 |
| noiseVariance | DSC-203 | 유한 수치, 자산의 modelVersion별 등록 범위 필요 |

등록되지 않은 지표 키 또는 코드 조합은 422다. fireAnomalyScore·noiseVariance는 최소 하나의 연결 자산에 해당 모델 버전의 범위가 등록돼야 하며, 존재하는 각 범위를 만족해야 한다. 측정할 수 없는 값은 생략한다.

### 8 4 파일 및 작업 상태

| 대상 | 상태 | 의미 |
|---|---|---|
| 미디어 | RECORDING / AVAILABLE / MISSING / NOT_APPLICABLE | 녹화 중 / 확정 파일 있음 / 원본 없음 / 비대상 |
| 작업 | QUEUED / UPLOADING / READY / FAILED / EXPIRED | 대기 / 점유 업로드 중 / 검증 완료 / 실패 / 접근 만료 |
| 사본 ACK | PRESENT / STORED / RETIRED | 현장 등록 / NAS 확정 완료 / 현장 배정 제외 |
| 로컬 outbox | PENDING / SENDING / ACKED / QUARANTINED | 전송 대기 / 전송 중 / 서버 ACK / 운영 조치 필요 |

### 8 5 기본 시간과 크기

| 항목 | 기본값 | 비고 |
|---|---|---|
| JSON 요청 상한 | 256 KiB | AWS 제어 본문 |
| 클립 전송 상한 | 512 MiB | 실제 MP4 파일 |
| heartbeat | 60초 | 신규 배정 건강 기준 180초, 관리 OFFLINE 5분 |
| claim 주기 | 5초 | 204이면 대기 |
| 작업 lease / 연장 | 5분 / 60초 | 전체 작업 기한 이내 |
| 작업 기한 / 시도 | 30분 / 최대 3회 | requestId 단위 |
| 전송 토큰 | 15분 | EC05 expiresAt 기준 |
| S3 PUT URL | 최대 15분 | 작업 잔여 기한으로 단축 가능 |
| 고객 GET URL | 최대 5분 | 실제 발급 응답 기준 |
| READY 접근 보관 | 24시간 | 접근 만료 후 안전한 시점에 객체 정리 |
| 현장 사본 | 기본 30일 | 추가 정리 조건 모두 필요 |
| 복제 반복 / API timeout | 30초 / 30초 | 영상 전송 기본 timeout 120초 |
| 미수락 배정 / 유지관리 | 60초 / 1분 | 실제 전환 지연은 주기·작업 상태 영향을 받음 |

설정값은 배포 환경에서 변경될 수 있다. 고정 시간으로 계산해 성공을 추정하지 말고 leaseExpiresAt·expiresAt·urlExpiresAt과 응답 상태를 확인한다. URL 발급만으로 실제 저장·업로드·다운로드 성공을 인정하지 않는다.

## 9 예제 사용과 검증

각 cURL 예제는 저장소 루트에서 실행한다. API_BASE_URL·ACCESS_TOKEN 및 필요 경로 ID를 실제 개발 환경으로 치환하고, 해당 요청 본문 예제를 ec01-request.json 같은 파일로 저장한 뒤 실행한다. 부록 예제 파일은 examples/edge-cloud-api/에 함께 제공한다. 토큰·서명 URL·leaseToken·transferToken은 앞 단계 응답에서 얻고 실제 값을 문서나 Git에 저장하지 않는다.

예제의 SIZE_BYTES와 checksumSha256은 설명용이다. 실제 파일에 대해 바이트 수와 SHA-256 Base64를 계산하고 request JSON과 S3 헤더를 모두 같은 값으로 바꾼다. 예제 JSON의 구문·필수 필드는 검증되지만 샘플 주소·토큰으로 실제 AWS 호출을 성공할 수 있다는 뜻은 아니다.

작성 시 AWS 제어 경로 13개와 OpenAPI의 메서드·요청 모델을 대조하고 요청 JSON을 실제 계약 모델로 검증했다. 응답·업무 오류·재요청 규칙은 service.py·replicas.py·api.py, 연관 바이트 전송은 archive.py·worker.py·replication.py와 대조한다. 실제 AWS·Cognito·NAS·현장 회선·부하 검수 결과는 별도 배포·검증 기록을 따른다.

문서 변경 시 계약 모델 또는 API 경로를 임의로 바꾸지 않는다. 원 계약 변경은 연동 규격·OpenAPI·구현·Markdown·Word를 함께 검토한다. 이 문서는 API와 DB 제품을 새로 설계하거나 배포하는 변경을 포함하지 않는다.
