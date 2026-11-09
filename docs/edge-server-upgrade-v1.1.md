# 엣지 서버 연동 개정 및 전환 절차

2026년 11월 9일 개정. 제품 명칭을 엣지 서버(Edge Server)로 통일한다. v1.1은 기능 추가보다 연동 이름 변경에 집중한 개정이지만, 기존 클라이언트는 수정 없이 새 API를 사용할 수 없다.

| 구분 | v1.0 | v1.1 |
|---|---|---|
| 작업·상태 보고 API | `/agent/jobs/*`, `/agent/heartbeat` | `/edge-server/jobs/*`, `/edge-server/heartbeat` |
| 응답·로그 식별자 | `agentId` | `edgeServerId` |
| 상태 보고 | `agentVersion`, `schemaVersion: "1.0"` | `edgeServerVersion`, `schemaVersion: "1.1"` |
| OAuth 작업 스코프 | `dsc/agent.work` | `dsc/edge-server.work` |
| 엣지 서버 설정 | `DSC_EDGE_AGENT_ID` | `DSC_EDGE_SERVER_ID` |
| 등록 JSON | `agents[].agentId` | `edgeServers[].edgeServerId` |
| 관리·실행 명령 | `dsc-admin agent-status`, `dsc-edge` | `dsc-admin edge-server-status`, `dsc-edge-server` |
| Linux 서비스 | `dsc-edge.service` | `dsc-edge-server.service` |
| 클라우드 DB | `agents`, `agent_id` | `edge_servers`, `edge_server_id` |
| 신규 큐·스택 출력 | `-agent-*`, `InitialAgentQueueUrl`, `InitialEdgeClientId` | `-edge-server-*`, `InitialEdgeServerQueueUrl`, `InitialEdgeServerClientId` |

`POST /ingest`의 이벤트 본문은 그대로다. 이벤트·클립 식별자, 자산 연결, 설치 ID 값, NAS와 outbox 파일은 유지한다. 예를 들어 이미 등록된 설치 ID가 예전 접두사로 시작하더라도 값은 유지하고 새 이름의 필드와 설정에 넣는다. 신규 설치 예제는 `edge-server-initial`을 쓴다.

## 기존 환경 전환

1. 점검 시간을 잡아 신규 작업 접수를 멈추고, 기존 `QUEUED`·`UPLOADING` 작업을 종료한 뒤 구버전 엣지 서버와 백엔드 정리 작업을 중지한다. 기존 큐와 DB·현장 SQLite의 백업을 확보한다. 기존 프로세스와 새 서비스를 동시에 실행하지 않는다.
2. 마이그레이션 계정으로 `alembic upgrade head`와 `deploy/database/grants.sql`을 적용한다. 사설 개발 DB는 새 릴리스의 임시 DB 초기화 함수로 실행할 수 있다. 마이그레이션은 테이블·열과 인덱스 이름, 저장된 스코프 및 상태 보고 키를 바꾼다. 이벤트·작업·클립과 연결 관계는 삭제하지 않는다.
3. 새 백엔드 코드와 SAM 설정을 함께 배포한다. 기존 개발의 감사 모드와 API 주소 파라미터를 유지한다. Cognito에서 새 스코프를 받은 토큰을 다시 발급한다. 초기 엣지 서버의 client ID·큐 URL은 새 스택 출력을 확인한다. 추가 엣지 서버용 큐와 앱 클라이언트도 새 이름과 스코프에 맞추고 DB의 큐 URL을 갱신한다. 이전 큐의 이름이나 저장된 URL은 DB 마이그레이션이 일괄 변경하지 않는다.
4. 기존 설정의 설치 ID 값을 `DSC_EDGE_SERVER_ID`에 옮기고, 기존 상태 디렉터리와 NAS 경로를 유지한 채 새 서비스로 전환한다. 미전송 이벤트는 같은 ID로 다시 전송한다.
5. 새 토큰으로 상태 보고·이벤트 수신·조회·클립 작업을 시험한다. 실패한 경우 새 트래픽을 열지 않고, 사전 백업과 기존 큐를 기준으로 DB·백엔드·엣지 서버를 함께 복구한다. 이전 큐의 메시지와 실패 보관 내역을 확인한 뒤 별도로 정리한다. 임시 DB 초기화 함수는 사용 후 제거한다.

규격서 변경 이력은 [연동 규격서 v1.1](aws-backend-edge-server-spec-v1.1.md)에 포함한다. 이전 발행본은 Git 이력에서 확인할 수 있다.
