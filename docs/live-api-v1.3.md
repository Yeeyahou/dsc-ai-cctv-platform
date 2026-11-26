# DSC 실시간 시청 API 명세

v1.3 추가 API의 요청·응답과 오류를 정의한다. 기존 엣지 제어 API 13개는 edge-cloud-api-v1.2.md를 유지한다. 아래 LA01~LA03은 직원 Cognito Bearer, LA04~LA05는 등록 field M2M Bearer를 요구한다. 모든 실제 AWS 호출은 HTTPS이며 영상 바이트는 이 API에 보내지 않는다. 예제는 비밀이 아닌 합성 값이다.

## 인증과 기본 동작

직원은 dsc/live.read·활성 등록·video=true·카메라 센터 또는 자산 읽기 권한을 모두 충족해야 한다. 현장 게이트웨이는 dsc/edge-server.work와 liveCameras에 등록된 field만 사용한다. 시청 capability를 Cognito Bearer 대신 AWS 인증 헤더에 넣지 않는다. 게이트웨이의 WHEP 헤더에만 capability를 보낸다. 응답은 no-store이며 토큰 원문을 로그에 남기지 않는다.

## API 상세

### LA01 GET /live/cameras

요청 필드

요청 본문 없음. 쿼리 없음. items는 자산 ID 순서이고 허용 카메라가 없으면 빈 배열이다. 현재 화면은 응답 목록 중 하나를 선택한다.

응답 필드

items 배열의 assetId·siteId·displayName은 문자열이다.

200 응답 예제

```json
{
  "items": [
    {
      "assetId": "camera-1",
      "siteId": "site-1",
      "displayName": "Camera 1"
    }
  ]
}
```

### LA02 POST /live/cameras/{asset_id}/sessions

요청 필드

본문은 빈 객체 {}이며 추가 필드를 거부한다. 경로 asset_id는 등록 자산 식별자다. DB 커밋 뒤 201을 반환한다.

```json
{}
```

응답 필드

sessionId UUID, assetId 문자열, sessionToken 비밀 capability, expiresAt UTC ISO 8601, whepUrl 운영자가 등록한 HTTPS WHEP 주소다. TTL은 기본300초와 발급 로그인 만료 중 빠른 시각이다. 새 시청 요청마다 새 세션을 만든다.

201 응답 예제

```json
{
  "sessionId": "57a9406e-7d14-4bd8-9cd4-88ca3706c98a",
  "assetId": "camera-1",
  "sessionToken": "57a9406e-7d14-4bd8-9cd4-88ca3706c98a.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "expiresAt": "2026-11-21T10:05:00.000Z",
  "whepUrl": "https://live.example.internal/media/camera-1/whep"
}
```

### LA03 POST /live/sessions/{session_id}/stop

요청 필드

본문 {}와 UUID 경로 session_id를 사용한다. 소유자만 종료하며 같은 소유자의 반복 호출은 성공한다.

```json
{}
```

응답 필드

sessionId UUID와 status=STOPPED다. 연결 종료는 게이트웨이의 다음 검사와 처리 시간 이후에 이루어진다. 이미 만료된 세션도 소유자가 종료할 수 있다.

200 응답 예제

```json
{
  "sessionId": "57a9406e-7d14-4bd8-9cd4-88ca3706c98a",
  "status": "STOPPED"
}
```

### LA04 POST /edge-server/live/authorize

요청 필드

token은 LA02의 sessionToken 원문이다. path는 등록 MediaMTX 카메라 경로이며 영숫자로 시작하는 영숫자·밑줄·대시 1~64자다. gateway가 URL query 없이 받은 capability와 실제 읽기 path를 보낸다.

```json
{
  "token": "57a9406e-7d14-4bd8-9cd4-88ca3706c98a.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "path": "camera-1"
}
```

응답 필드

sessionId UUID, expiresAt UTC ISO 8601. 다른 field·다른 path·다른 토큰은 404다. 이 확인 성공만으로 미디어 프레임 수신 성공을 뜻하지 않는다.

200 응답 예제

```json
{
  "sessionId": "57a9406e-7d14-4bd8-9cd4-88ca3706c98a",
  "expiresAt": "2026-11-21T10:05:00.000Z"
}
```

### LA05 POST /edge-server/live/check

요청 필드

sessionIds는 UUID 배열이며 최대64개, 빈 배열도 허용한다. 게이트웨이가 자기 peer 레지스트리의 ID만 보낸다.

```json
{
  "sessionIds": [
    "57a9406e-7d14-4bd8-9cd4-88ca3706c98a"
  ]
}
```

응답 필드

activeSessionIds에는 현재도 유효하고 해당 field에 속한 UUID만 포함한다. 만료·회수·권한 상실·다른 field·미등록 세션은 생략한다. AWS 확인 실패 시 게이트웨이는 현재 연결을 종료한다.

200 응답 예제

```json
{
  "activeSessionIds": [
    "57a9406e-7d14-4bd8-9cd4-88ca3706c98a"
  ]
}
```

## 오류와 재요청

| HTTP | code | 처리 |
|---|---|---|
| 401 | AUTH_REQUIRED / INVALID_TOKEN / INACTIVE_PRINCIPAL | 새 로그인 또는 설치 인증·등록 확인 |
| 403 | INSUFFICIENT_SCOPE | scope 등록 조정, 자동 무한 재시도 금지 |
| 404 | NOT_FOUND | 권한 없는 카메라·다른 소유자·잘못된 capability도 포함, 원문 비밀 출력 금지 |
| 422 | VALIDATION_ERROR | JSON 필드·UUID·path·배열 상한 확인 |
| 503 | LIVE_DISABLED | 기능 활성화 및 설치 상태 확인 |
| 503 | STORAGE_UNAVAILABLE | DB 복구 후 제한된 재시도 |

오류 본문은 기존 error.code·message·traceId·retryable을 따른다. body가 JSON 문법 오류이면 400 INVALID_JSON, 256KiB 초과이면 413 BODY_TOO_LARGE다. 토큰 검증은 매 호출 수행한다. 시청 시작 응답을 받지 못해 재시도하면 다른 세션이 생길 수 있으며 이전 세션은 TTL로 만료한다. stop은 동일 ID로 반복할 수 있다.

## 미디어 경계

브라우저는 LA02의 whepUrl에 application/sdp POST와 Authorization Bearer sessionToken을 보낸다. 성공한 201 SDP 응답과 Location의 세션 리소스를 사용하고 종료 시 DELETE한다. 현재 플레이어는 ICE 수집 완료 후 SDP를 보내며 별도 trickle PATCH는 필요하지 않는다. 동적 ICE restart·음성·PTZ·TURN·HLS는 제공하지 않는다. 연결 장애 때 사용자가 다시 보기를 누르면 기존 peer와 시청 세션을 정리하고 새로 시작한다. 설정·VPN·TLS·브라우저 호환성과 검수는 streaming-installation-guide.md를 따른다.
