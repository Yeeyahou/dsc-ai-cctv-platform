# v1.2 개발 배포와 실연동 검증 기록

검증일은 2026년 11월 11일이다. CCTV·로봇 공통 field 엣지와 사무실 archive 수신기·업로더를 구현하고 기존 AWS 개발 스택을 갱신했다. 물리 NAS와 현장 PC 설치는 장비·접속 정보가 준비되지 않아 아직 수행하지 않았다. 설치와 복구 절차는 [운영 안내](office-worker-operations.md), 계약과 검수 항목은 [v1.2 규격](aws-backend-edge-server-spec-v1.2.md)을 따른다.

## 배포 대상과 산출물

| 항목 | 확인 결과 |
|---|---|
| AWS 대상 | 계정 `955268448422`, `ap-northeast-2`, 개발 스택 `dsc-dev-app` |
| API | `https://wetuhtamrg.execute-api.ap-northeast-2.amazonaws.com/dev` |
| 실행 소스 커밋 | `d1c2e0f2d6d53dc3151812894dac10658e436a8a` |
| 앱 ZIP SHA-256 | `59ac2b3c918da23d71d16e4fe2d0cea2c3b24c07dc4f6e9547642c1748ef1520` |
| DB 초기화 ZIP SHA-256 | `c9988eb1c794a29171e6c7aeebfac16d949814a6a53c0a282c4b131cdf44d62e` |
| 스키마 | `7c195c8eabb1` → `952701c2d6ef` |
| 복구 지점 | 암호화 RDS 스냅샷 `dsc-dev-office-v12-20261111`, `available` 확인·보존 |
| 배포 후 상태 | 앱 `UPDATE_COMPLETE`, API·유지관리 Lambda `Active` / `Successful` |

앱 ZIP은 아티팩트 버킷의 `releases/office-v1.2/<SHA-256>/app.zip`에 보관했다. API와 유지관리 Lambda의 `CodeSha256`은 모두 위 ZIP 해시의 Base64 표현과 일치했다. ZIP 안의 실행 소스 35개 파일도 최종 작업 소스와 바이트 단위로 대조했다. 이후 변경은 배포 기록과 설정 예제 식별자 정렬이며 실행 소스를 바꾸지 않았다.

기존 설치 `agent-initial`은 legacy 역할로 보존했다. `InitialQueueNaming=agent`로 기존 물리 큐 `dsc-dev-app-agent-initial`과 DLQ를 유지했다. 변경 세트에서 기존 작업 큐·DLQ·Cognito 사용자 풀·클립 버킷·런타임 비밀의 교체가 없음을 확인했다. CCTV·로봇·사무실용 별도 Cognito client와 큐는 생성했으며 실제 설치 주체 등록은 장비 정보 확정 후 수행한다. 개발 환경은 `ClipAuditMode=synthetic`을 유지한다.

## 기존 데이터 보존

마이그레이션 직전과 직후에 같은 조회로 아래 건수와 이벤트·자산 연결·영상 메타데이터의 정렬된 내용 해시를 비교했다.

| 테이블 | 직전 | 직후 |
|---|---:|---:|
| events | 2 | 2 |
| event_assets | 2 | 2 |
| event_media | 2 | 2 |
| file_requests | 1 | 1 |
| job_leases | 3 | 3 |

양쪽 내용 해시는 `85be7ce33a90e472e56302af5220c3151e4da2932da38d1d145675bbdd4324b2`로 같았다. 기존 client의 활성 상태·센터·자산·영상 권한을 보존하고 이전 작업 scope만 새 이름으로 전환했다. 아래 연동 시험에서 생성한 별도 합성 데이터는 감사 목적으로 남겼으므로 시험 후 전체 건수와 전체 내용 해시는 증가·변경됐다.

## 자동 검사와 패키지 검증

- 외부 AWS 호출을 격리한 기본 pytest: 102건 통과, PostgreSQL 전용 8건 생략.
- 전용 PostgreSQL 17 전체 pytest: 110건 통과. 주·예비 동시 claim, 정리와 요청의 경쟁, 이전 데이터 마이그레이션 및 제한된 런타임 DB 권한을 포함한다.
- Ruff 정적·포맷 검사 50개 파일, 모든 CloudFormation 템플릿 cfn-lint와 CloudFormation Guard 3.2.1 보안 규칙 통과.
- `alembic upgrade head`와 `alembic check` 통과, 모델 차이 없음.
- Linux/arm64 이미지 `dsc-edge-server:1.2.0` 빌드, 수신기·워커 모듈 import 및 office Compose 설정 검증 통과. 다른 CPU 아키텍처 이미지는 해당 대상에서 별도 빌드·검증한다.
- 같은 Markdown에서 작성한 Word 규격 22페이지 전체를 렌더링해 한글·그림·표·페이지 번호 확인.

기존 Starlette TestClient의 HTTPX 폐기 예정 경고 1건이 남아 있다. 시험 결과와 항목별 적용 범위는 [검증 기록](verification.md)에 연결했다.

## 실제 AWS 연동

실제 Cognito 토큰, API Gateway, Lambda, PostgreSQL, SQS와 S3를 사용했다. 격리된 합성 센터 `verify-v12-c224798e32`와 임시 client 4개·큐 3개를 사용했으며 기존 설치 권한은 수정하지 않았다. 현장 inbox 접수·outbox 전송·사무실 복제·작업 업로드는 제품의 실행 함수를 사용했다.

사무실 수신기의 HTTP 경계만 로컬 ASGI TestClient로 연결하고 NAS는 로컬 디렉터리로 대체했다. 현장→사무실의 실제 WAN·TLS·NAS 장치 동작을 검증한 결과로 해석하지 않는다. 다른 AWS 통신은 실제 HTTPS였다.

시험 입력은 합성 H.264 MP4 2,319바이트이며 SHA-256은 `38cb0ba9497e16cdd7f11101e9f8b83e12bf5e1f77acc0420d6ac70bdb12ac62`다.

| 시나리오 | requestId | 결과 |
|---|---|---|
| 사무실 정상 주 업로드 | `096eec4e-5d70-4a7b-bc3f-6cd4ab15e3fb` | READY, 다운로드 원본 바이트 일치 |
| 사무실 NAS 비정상 보고 후 로봇 예비 업로드 | `1e410ec6-5a02-4b7b-9854-bc263ad2e5f4` | READY, 다운로드 원본 바이트 일치 |
| 사무실 claim 후 SOURCE_UNREACHABLE 실패, CCTV 재배정 | `42e52f09-1dfa-4bb5-b04d-e5fbd91f5a82` | 두 번째 시도 READY, 이전 office lease 완료 통지 404, 다운로드 원본 바이트 일치 |

같은 이벤트 재전송은 HTTP 200과 중복 확인을 반환했고 위조 `siteId`는 403으로 거부했다. API가 발급한 실제 5분 다운로드 서명 URL은 만료 후 S3에서 HTTP 403과 만료 응답을 반환했다. 시계를 앞당긴 모의 시험과 구분한다.

## 시험 자원 정리

합성 영상 3건을 MISSING revision 1로 보고하고 시험 주체 4개를 비활성화했다. 임시 Cognito client 4개와 SQS 큐 3개를 삭제했으며 추가 client 허용 목록을 원래 값 `[]`로 복구했다. 합성 이벤트·작업 이력은 감사 기록으로 보존한다. 개발 앱과 DB 및 복구 스냅샷은 유지한다. 임시 DB 초기화 스택의 삭제 완료는 최종 배포 확인에 포함한다.

## 남은 실제 설치와 인수

NAS가 도착하면 OS·CPU와 공유 파일시스템의 descriptor 탐색·hard link·파일 및 디렉터리 fsync·전원 복구를 검수한다. CCTV·로봇·사무실의 접속 주소·설치 계정·SSH 키 경로, 저장 루트와 NAS 마운트, HTTPS 호스트·인증서·VPN 또는 수신 경로, 실제 센터·자산 담당 관계가 필요하다. 준비 정보와 설치 파일은 [운영 안내](office-worker-operations.md)에 있다.

현장 PC의 관리 사본은 기본 30일이 지나고 동일 NAS 사본의 크기·해시 재검증과 진행 중 작업 보호를 만족한 뒤 정리한다. NAS 미확인 클립과 미전송 outbox는 보존하므로 장애가 길면 디스크가 계속 증가한다. 이 정리는 생성 프로그램이 소유한 원본을 삭제하지 않는다. 같은 디스크에 원본과 관리 사본을 두면 두 파일의 용량을 모두 산정하고 원본 정리는 생성 프로그램에서 별도로 설정한다.

실제 회선·전원·디스크 장애, 현장→AWS 독립 경로, 장기 장애 누적량·30일 보관 용량·부하, NAS 백업 복원과 RPO/RTO 측정은 AT25 현장 검수로 남아 있다. 사무실과 현장 사본의 동시 손실이나 현장 보관 기간 밖 클립의 가용성은 별도 NAS 백업 범위에 따른다. 이번 배포는 개발 환경이며 `main` 승격·운영 배포를 수행하지 않았다.
