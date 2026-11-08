# 구현 결정

## 실행과 저장

Python 3.12를 백엔드와 Linux 엣지의 공통 기준으로 사용한다. FastAPI 앱을 Mangum으로 Lambda 이벤트에 연결하며, 로컬에서도 같은 라우트와 업무 로직을 실행한다. 인증 검증기는 테스트에서만 주입한다.

DB는 PostgreSQL 17을 선택했다. 이벤트·자산 연결·초기 클립은 하나의 트랜잭션으로 저장한다. 이벤트 행과 영상 작업 행의 PostgreSQL 행 잠금을 사용해 활성 작업 생성과 lease 갱신을 직렬화한다. SQLite는 로컬 테스트 및 에이전트 저장에만 사용하며 운영 백엔드 설정에서 거부한다. 실제 배포의 PostgreSQL minor 버전은 리전 지원 여부를 확인한다.

DB 계정은 마이그레이션·등록을 수행하는 운영 계정과 API 실행 계정으로 분리한다. 실행 계정은 자산·권한을 읽기만 하고 heartbeat 열 및 이벤트·작업 데이터에 필요한 쓰기만 수행한다.

## 영상 큐와 복구

설치마다 별도 SQS 큐를 둔다. 여러 센터의 작업이 하나의 큐에 섞여 다른 에이전트가 반복해서 가져가는 상황을 피하기 위한 구성이다. 큐 URL은 서버의 agent 등록에만 보관한다.

작업의 dispatch version과 DB 전송 표식을 이용해 DB 저장 후 SQS 전송 전 장애를 복구한다. DB 상태가 기준이며 SQS는 배정 전달에 사용한다. 큐 전달이 중복되어도 작업 행 잠금과 현재 시도 번호·토큰 해시로 유효 lease를 하나만 발급한다. 유지보수 함수는 미전달 작업, lease 만료와 접근 보관 만료를 처리한다.

시도별 객체 키는 서버가 정하며 업로드 조건은 같은 시도 안에서 바꿀 수 없다. S3 PUT에는 크기·형식·SHA-256 조건을 서명한다. 완료는 HEAD의 실제 크기·형식·ChecksumSHA256으로 확인한다. 해시가 같아야 하므로 발급된 PUT URL 재사용으로 다른 영상으로 교체할 수 없다.

이전 시도의 객체는 해당 작업의 마지막 PUT URL이 만료된 뒤 정리한다. READY 영상은 접근 보관 만료 후 정리하며 삭제 성공 시각을 저장한다. Lifecycle의 3일 규칙은 정리 누락에 대비한 보조 장치다.

## 현장 범위

현재 에이전트의 지원 실행 환경은 Python 3.12가 설치된 POSIX 호스트이며 운영 서비스 예시는 Linux systemd다. Windows 서비스 지원을 구현한 것으로 표시하지 않는다. NAS는 운영자가 읽기 전용으로 마운트한다. 열린 디렉터리 descriptor를 기준으로 파일을 찾아 심볼릭 링크와 경로 교체를 통한 루트 이탈을 막는다.

outbox·manifest·입력 receipt는 SQLite WAL과 FULL 동기화로 저장한다. 전송 중 종료된 건은 같은 ID·본문으로 복구하며 클립 갱신은 이벤트 ACK 및 이전 revision ACK 이후에 전송한다. 로컬 접수와 서버 ACK를 분리한다. 녹화·AI 모델의 사건 식별은 생산 프로그램의 책임이며 에이전트는 수신한 incidentId를 보존한다.

## AAS 범위

API 계약 버전은 3.1.2로 유지한다. Shell과 Submodel은 고정된 IDTA metamodel JSON Schema 3.1.2로 검증한다. Nameplate·TechnicalData의 실제 값은 등록한 자산 메타데이터에서만 가져오며 TimeSeries는 같은 이벤트 DB의 통계와 인증된 이력 URL을 제공한다.

현재 구현은 규격서에 나열된 읽기 경로의 부분 집합이며 전체 IDTA 서비스 프로파일 충족을 선언하지 않는다. `/description`의 `profiles`는 빈 배열이다. `properties-v1.json`은 프로젝트의 고정 필드 매핑이며 공식 IDTA Nameplate·TimeSeries 템플릿 전체 구현이라는 의미가 아니다. 실제 소비자와의 프로파일·semanticId 합의는 등록 매니페스트에 반영한다.

## 배포 경계

SAM 앱 스택은 기존 VPC·사설 서브넷·PostgreSQL 런타임 secret·ACM 인증서·Route 53 zone을 입력받는다. 선택적인 DB 스택은 같은 VPC에 사설 RDS를 만든다. 회사 네트워크와 운영 연락처는 임의로 생성하지 않는다.

직원용 public client는 secret 없이 code flow를 사용한다. 로그인 클라이언트는 PKCE를 적용해야 한다. 회사 SAML/OIDC IdP는 회사 제공 메타데이터로 연결한다. 초기 파트너·에이전트 client 외 설치를 추가하면 별도 client·큐를 만들고 allowlist와 DB 등록을 함께 갱신한다.
