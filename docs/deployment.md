# 배포 및 운영 안내

v1.0 설치의 엣지 서버 식별자·스코프 전환은 [v1.1 전환 절차](edge-server-upgrade-v1.1.md)를 따른다.

## 환경 준비

운영·개발 AWS 계정과 저장소를 분리한다. 운영 매니페스트에는 리전, 도메인·인증서, VPC·사설 서브넷, DB secret ARN, 센터·자산·엣지 서버, 회사 IdP, 보관 정책과 장애 수신자를 기록한다. 실제 비밀번호·client secret·서명 URL은 Git에 넣지 않는다.

백엔드의 사설 서브넷은 PostgreSQL 외에 Cognito JWKS·Secrets Manager·SQS·S3로 HTTPS 접속할 수 있어야 한다. 사내 기존 NAT·프록시 또는 지원되는 VPC endpoint를 구성한다. Cognito 공개 JWKS 접근을 포함해 연결을 시험한다. 이 저장소는 회사 VPC 라우팅을 임의로 변경하지 않는다.

## PostgreSQL

기존 PostgreSQL이 있으면 이벤트용 DB와 보안 그룹을 입력으로 사용한다. 새 RDS가 필요하면 `infra/database.yaml`을 배포한다. 템플릿은 비공개·암호화·TLS 강제·14일 백업·삭제 보호를 사용한다. 기본 Multi-AZ를 비용만을 이유로 조용히 해제하지 않는다. 리전에서 지원되는 PostgreSQL 17 minor 버전을 확인한다.

사설 관리 호스트에서 `deploy/database/roles.sql`을 최초 한 번 적용한다. 마이그레이션 계정으로 `alembic upgrade head`를 실행하고 `deploy/database/grants.sql`을 적용한다. 등록 정보도 마이그레이션·운영 계정으로 입력한다. API에 RDS master 계정을 사용하지 않는다.

런타임 계정은 이벤트를 수정하거나 감사 기록을 읽을 수 없다. 클립 상태 갱신과 작업 생성은 이벤트 ID에 대한 PostgreSQL 트랜잭션 잠금으로 직렬화한다. 감사 기록은 생성된 행을 다시 읽지 않고 추가한다.

Secrets Manager에는 역할별로 다음 JSON 키를 가진 secret을 만든다. 아래는 형식 설명이며 실제 비밀 파일은 저장소 외부의 접근 제한 위치에서 관리한다.

```json
{"username":"dsc_runtime","password":"REPLACE_SECURELY","host":"DB_ENDPOINT","port":5432,"dbname":"dsc"}
```

앱은 패키지에 포함한 AWS RDS CA 묶음으로 `sslmode=verify-full`을 사용한다. CA는 AWS 공식 truststore에서 받은 것이며 운영 인증서 전환 전 갱신·검증한다. 마이그레이션은 `DSC_DATABASE_SECRET_ARN` 또는 TLS 옵션이 있는 `DSC_DATABASE_URL`로 지정한다. 로컬 전용 컨테이너에서는 SSL 없는 테스트 URL을 사용할 수 있다.

## 앱 스택

AWS SAM CLI와 Docker가 설치된 빌드 환경에서 실행한다.

```sh
sam build --use-container --template-file infra/template.yaml
sam deploy --guided
```

운영 매니페스트의 값을 파라미터로 입력한다. 빌드는 잠금 파일의 버전·해시로 Linux x86_64 패키지를 설치한다. Lambda 예약 동시 실행은 API 10, 정리 작업 1로 시작한다. DB 연결 수·실제 부하를 측정한 뒤 조정한다.

API Gateway CloudWatch 역할은 리전·계정 전체 설정이다. 기존 `AWS::ApiGateway::Account`를 관리 중이면 중복 스택을 만들지 말고 `ApiAccount`와 `ApiLogRole`을 기존 구성에 통합하거나 가져온다. `DatabaseIngress`는 지정 DB 보안 그룹에 앱 Lambda 보안 그룹만 추가한다.

S3는 비공개·HTTPS·SSE-S3·ACL 비활성화를 적용한다. 클립 버킷의 versioning은 규격에 따라 중단하고 감사 버킷은 versioning을 유지한다. CloudTrail 데이터 이벤트와 S3 접근 로그는 감사 버킷에 보관한다. 감사 로그의 실제 열람 권한과 보관 기간은 회사 운영 정책으로 설정한다.

템플릿의 CloudWatch 경보는 수신자를 임의로 지정하지 않는다. 운영 매니페스트의 담당자·SNS 또는 기존 관제 수신 경로에 연결하고, 시험 오류로 전달·확인까지 검증한다. API 5xx, Lambda 오류, 유지보수 실패 외에 DB의 최근 heartbeat·대기 건수와 현장 디스크 사용량을 관제한다.

## Cognito와 등록

출력되는 직원 client ID, 초기 엣지 서버 client ID, 파트너 client ID와 큐 URL을 등록 매니페스트에 반영한다. client secret은 출력·문서 복사 대신 관리 콘솔 또는 승인된 비밀 전달 경로로 현장에 설치한다.

직원용 회사 SAML/OIDC IdP를 User Pool에 등록한 뒤 `StaffIdentityProvider`를 해당 provider 이름으로 변경한다. 초기 `COGNITO` 값은 스택 준비용이다. 운영 전 회사 계정 로그인·PKCE·퇴사자 권한 회수를 시험한다. 직원은 `user:<sub>`, 서버·파트너·엣지 서버는 `client:<client_id>`로 DB 주체를 구분한다.

```sh
dsc-admin --env-file /secure/operator.env register deployment.local.json
dsc-admin --env-file /secure/operator.env edge-server-status
```

`examples/registration.json`의 샘플 자산과 식별자를 실제 값으로 바꾼다. 등록은 한 DB 트랜잭션으로 적용한다. 권한 등록을 생략한 신규 주체는 접근할 수 없다. 파트너는 허용 자산의 Nameplate·TechnicalData부터 시작하고 이력·영상은 별도 허용한다.

설치를 추가할 때 Cognito app client와 SQS 큐를 각각 만든다. 큐는 `${앱스택명}-edge-server-...` 접두사, SSE-SQS, visibility 300초, 보존 1일, 동일 보안 정책과 DLQ를 적용한다. `AdditionalClientIdsJson` 파라미터에 client ID를 추가하고 DB 엣지 서버 등록에 해당 큐 URL을 입력한다. 직원 client secret을 엣지 서버에 재사용하지 않는다.

## 현장 설치와 복구

Python 3.12 가상 환경에 잠금된 패키지를 설치하고 `/etc/dsc/edge.env`, `0600` client secret, 읽기 전용 NAS 마운트를 준비한다. `dsc-edge` 사용자가 `/var/lib/dsc`, `/var/spool/dsc`에 쓸 수 있어야 한다. inbox에는 승인된 AI·녹화 프로그램만 쓰도록 권한을 설정한다.

`deploy/edge/dsc-edge-server.service`의 설치 경로와 NAS 경로를 현장 값에 맞춘다. 등록 후 systemd에서 자동 시작과 실패 재시작을 확인한다. 실행 중 업데이트·백업에는 outbox DB와 WAL을 함께 다루거나 SQLite backup API·서비스 중지 상태의 일관된 백업을 사용한다. 배포·롤백 때 상태 디렉터리를 새로 만들지 않는다.

인터넷 단절·종료 후에는 outbox가 남고 같은 ID로 재전송된다. 격리 건은 원인을 해결한 뒤 `--retry`로 재처리한다. 디스크 부족 시 미전송 건을 삭제하지 않고 용량을 확보한다. NAS 오류는 메타데이터 조회와 별개로 처리한다.

## 운영 인수

검수 AT01~AT17의 실제 환경 확인을 완료한 뒤 운영으로 전환한다. 특히 회사 IdP·파트너 소비자 AAS·NAS 마운트·S3 직접 업로드·큐 장애 복구·관제 알림·백업 복원을 시험한다. 센터 수, 이벤트율, 동시 클립 수, NAS 보관 기간, API 지연 기준과 RPO·RTO는 회사 매니페스트에 실제 수치로 기록한다.

본 저장소의 커밋·푸시만으로 AWS 배포나 현장 검수가 완료된 것은 아니다. 개발 시험과 실제 환경의 측정 결과를 구분해 보관한다.

## 사무실 NAS와 현장 예비 업로더

v1.2의 설치 역할·HTTPS 수신·NAS 볼륨 마커·30일 관리 사본과 장애 전환·정리 보호는 [사무실·현장 운영 안내](office-worker-operations.md)를 따른다. 새 CCTV·로봇·사무실 client와 큐는 앱 스택 출력에서 읽고 등록 매니페스트에 반영한다. 기존 legacy 설치는 명시적으로 전환할 때까지 이력과 큐를 보존한다.
