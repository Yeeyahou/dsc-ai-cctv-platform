# 사무실 NAS와 현장 예비 업로더 설치·복구

CCTV와 로봇 엣지는 같은 `dsc-edge-server` 실행 파일을 별도 설정으로 실행한다. 사무실에서는 `dsc-archive-receiver`가 클립을 NAS에 확정하고, `dsc-edge-server`의 archive 역할이 요청된 클립을 S3로 업로드한다. 두 사무실 프로세스는 같은 상태 디렉터리와 NAS 루트를 사용한다. 지원 OS는 Python 3.12와 POSIX 파일 기능을 제공하는 Linux다.

NAS 자체가 컨테이너·Python 실행을 지원하면 두 프로세스를 NAS 안에서 실행할 수 있다. 별도 PC에 실행할 때는 NAS 공유 디렉터리를 마운트한다. NAS 모델과 공유 프로토콜에서 실제 fsync·hard link·전원 복구를 검수한다. 수신 HTTPS 주소는 수신기를 가리키며 NAS 구입 여부와 독립적으로 준비할 수 있다.

## 준비할 정보

| 항목 | 용도 |
|---|---|
| CCTV·로봇 PC 및 워커 호스트의 OS·접속 주소·설치 계정·SSH 키 경로 | 설치와 서비스 자동 시작 |
| 현장 생산 파일 루트, 관리 사본 전용 경로와 여유 용량 | 30일 보관과 장애 누적량 확보 |
| NAS 쓰기용 공유 경로·계정, 서비스 전용 디렉터리 | 수신·읽기·검증·파일 확정 |
| 사무실 HTTPS 호스트, TLS 인증서 및 VPN 또는 외부 수신 경로 | 인증된 현장→사무실 클립 전송 |
| siteId·자산 및 각 설치의 담당 관계 | 센터·자산별 작업 권한과 배정 |
| 각 설치의 Cognito client 및 비밀 파일 | 개별 인증·폐기·교체 |

비밀은 채팅·Git·설정 예제에 쓰지 않고 권한 0600 파일로 공급한다. 사무실 수신기는 기본 127.0.0.1:8080에 바인딩한다. 역방향 프록시가 TLS를 종료하고 이 루프백 주소로 전달한다. NAS 관리·SMB·NFS 포트를 외부에 공개하지 않는다. VPN 주소를 사용해도 현장 allowlist와 인증서 검증을 적용한다.

## 저장 공간 준비

현장 `nasRoot`는 생산 프로그램이 완성한 클립을 읽는 입력 루트다. field 역할은 이 파일을 `cacheRoot` 아래 관리 사본으로 확정한다. 생산 프로그램은 수신 JSON을 임시 파일로 작성하고 fsync·rename 후 제공하며, accepted 확인 전까지 해당 클립을 변경하거나 정리하지 않는다. 관리 사본은 자체 보관 정책으로 정리한다. 생산 프로그램이 가진 원본의 정리는 생산 프로그램 운영자가 설정한다.

`cacheRoot`에는 하루 최대 클립 용량 × 30일과 NAS 전송 장애 누적량, 여유 공간이 필요하다. 원본과 관리 사본을 같은 디스크에 둘 경우 두 파일의 용량을 합산한다. outbox·SQLite 상태 저장 영역은 별도 여유 공간을 확보한다. 30일 보관만으로 디스크 최대 사용량이 제한되지는 않는다.

사무실 NAS에 서비스 계정만 쓰는 루트를 만들고, 실제 마운트된 볼륨 안에 `.dsc-volume-id` 파일을 생성한다. 예를 들어 `dsc-office-incheon-01`을 기록하고 `DSC_EDGE_ARCHIVE_VOLUME_ID`와 일치시킨다. 마운트가 빠진 호스트의 빈 디렉터리에 같은 마커를 만들지 않는다. 마커는 운영자가 준비하고 서비스는 자동 생성하지 않는다. 수신 도중 볼륨이 바뀌어도 확인 ACK를 반환하지 않는다.

NAS 루트는 임시 파일과 확정 파일 사이의 같은 디렉터리 hard link, 파일 및 디렉터리 fsync, 안전한 POSIX descriptor 탐색을 지원해야 한다. 현장 전용 관리 영역과 NAS 수신 영역의 다른 프로그램 쓰기를 제한한다. 실제 NAS가 이러한 동작을 지원하는지 설치 전에 확인한다.

## 설치와 등록

1. 기능 PR 검사 후 `develop`의 검증된 소스와 잠금 파일을 사용한다. Python 3.12 환경에서 `requirements.lock`을 해시 검증으로 설치하고 프로젝트를 설치한다.
2. `examples/field.env.example`을 CCTV·로봇별로 복사한다. 각각 serverId·clientId·secretFile·stateDir·inbox·cacheRoot를 다르게 설정한다. 같은 센터의 siteId는 같게 둔다.
3. 사무실은 `examples/office.env.example`을 사용한다. `role=archive`, NAS 루트·마커와 수신 주소를 설정한다. 수신기와 업로더가 같은 환경 파일을 읽게 한다.
4. `examples/office-registration.json`을 실제 AWS 출력과 자산으로 채운다. 관리 계정의 `dsc-admin register` 또는 개발용 임시 초기화 함수로 적용한다. office 자산은 담당 CCTV·로봇 이벤트의 모든 연결 자산을 포함해야 한다. field의 archiveServerId로 담당 사무실 워커를 지정한다.
5. `deploy/edge/dsc-edge-server@.service`로 cctv·robot·office 인스턴스를 실행한다. 사무실에는 `deploy/edge/dsc-archive-receiver.service`도 설치한다. 예제 경로와 서비스 계정에 맞게 디렉터리·환경 파일·비밀 파일 권한을 준비한다.
6. `deploy/edge/office-nginx.conf.example`의 호스트와 인증서 경로를 실제 값으로 바꿔 HTTPS를 연결한다. 프록시는 클립 본문을 로그에 남기지 않으며 최대 본문·시간 제한을 적용한다.
7. 합성 이벤트와 클립으로 NAS 복제, 사무실 claim, S3 크기·SHA-256 완료 검증과 다운로드를 확인한다. 사무실 워커·NAS·회선을 각각 중단하고 현장 전환을 확인한다.

```sh
python3.12 -m venv /opt/dsc/venv
/opt/dsc/venv/bin/pip install --require-hashes -r requirements.lock
/opt/dsc/venv/bin/pip install --no-build-isolation --no-deps .
sudo systemctl enable --now dsc-edge-server@cctv
sudo systemctl enable --now dsc-edge-server@robot
sudo systemctl enable --now dsc-edge-server@office dsc-archive-receiver
```

프로젝트 설치에는 빌드 도구 hatchling이 필요하다. 개발 잠금 파일에 포함된 도구를 별도 빌드 환경에서 설치하고 wheel을 만들어 현장에 공급할 수 있다. 운영 장비에서 의존성을 임의로 갱신하지 않는다.

## NAS가 도착하기 전 시험

전용 임시 디렉터리를 NAS 루트로 설정하고 합성 데이터용 마커를 만든다. 수신기·워커는 동일한 코드로 실행한다. 로컬 파일 확정과 복제 프로토콜을 검증할 수 있다. 실제 NAS 전원 차단·회선·마운트·성능·백업 복원은 장비 도착 후 AT25로 검수한다. AWS 개발 스택은 `ClipAuditMode=synthetic`이므로 합성 클립만 사용한다.

## 장애 중 동작

| 상황 | 보존과 처리 | 확인할 항목 |
|---|---|---|
| AWS 인터넷 단절 | 현장 outbox와 관리 사본 보관, 동일 ID·본문 재전송 | 디스크 여유와 인증, 복구 후 ACK 감소 |
| 사무실 전송 단절 | 현장 검증 사본으로 S3 작업 처리, NAS 복제 재시도 | 현장→AWS 독립 회선 및 미복제 건 |
| NAS 또는 사무실 워커 장애 | 건강한 현장 사본으로 배정 | heartbeat·마커·현장 사본 존재 |
| 미수락 작업 | 기본 60초 후 다른 사본으로 재배정 | 유지관리 함수의 1분 주기 추가 |
| 업로드 도중 워커 종료 | 유효 lease를 보호하고 기본 5분 만료 후 새 시도 | 이전 token·dispatchVersion·객체 키 차단 |
| NAS 파일 저장 후 ACK 유실 | 재검증 요청으로 같은 파일·해시와 서버 기록 일치 | 중복 파일·잘못된 STORED가 없는지 |
| NAS 내용 변조 | 해당 전송·업로드의 성공과 정리 차단 | 현장 사본을 보존하고 원인 확인 |
| 로컬 접수 중 디스크 오류 | 입력·미전송 기록 보존, 다른 입력과 기존 outbox 처리 계속 | 디스크 여유·파일시스템 오류 |
| 30일 경과 중 NAS 단절 | 현장 사본 보존 | 공간 경고와 NAS 복구 |
| 정리 중 종료·ACK 유실 | 서버 배정 차단과 로컬 RETIRING을 재조정 | 복구 후 파일·사본 상태 일치 |
| 양쪽 저장소 동시 손실 | 남은 S3 접근 기간 및 별도 백업의 범위에 따라 복구 | 실제 NAS 백업·RPO/RTO |

유효한 S3 클립의 조회와 AWS 이벤트 이력은 사무실 중단 중에도 제공된다. 현장에 남아 있는 보관 기간의 사본이 예비 업로드 범위다. 30일이 지난 뒤 사무실에만 남은 클립은 NAS와 백업의 가용성에 따른다. 사무실 회선이 현장→AWS 통신 경로까지 함께 끊는 구성은 독립 예비 경로 요건을 충족하지 않는다.

## 운영 점검과 재처리

```sh
# 조회·재처리 명령은 해당 인스턴스 서비스를 중지한 상태에서 실행한다.
dsc-edge-server --env-file /etc/dsc/cctv.env --inspect-outbox
dsc-edge-server --env-file /etc/dsc/cctv.env --inspect-copies
dsc-edge-server --env-file /etc/dsc/cctv.env --retry-copy EVENT_ID REVISION
```

공유 상태 저장소의 프로세스 잠금으로 동시에 두 발신 프로세스를 시작하지 못하게 한다. 원인을 해결하고 동일 식별자·크기·해시의 사본을 복구한 뒤 격리 건을 재시도한다. outbox·manifest·clip_copies DB를 새로 만들거나 미전송 파일을 임의 삭제해 상태를 초기화하지 않는다.

heartbeat는 field의 `replicaPendingCount`, `replicaQuarantinedCount`와 디스크 여유를 추가 보고한다. AWS의 격리·디스크 부족 경보는 보고된 상태를 관측한다. 경보 수신 채널과 확인 담당자는 운영 환경에서 연결한다. 회선 단절 중 현장 경고는 systemd journal로 확인한다.

## 마이그레이션과 롤백

v1.2 DB 변경은 새 역할·사본·전송 레코드와 작업 스냅샷을 추가한다. 기존 설치는 legacy로 보존한다. v1.0 스택을 갱신할 때는 `InitialQueueNaming=agent`로 기존 물리 큐를 보존하고 초기 설치 등록도 해당 큐를 유지한다. 새 CCTV·로봇·사무실 큐는 서로 분리한다.

DB 백업·전환 기록을 남기고 임시 초기화 함수로 Alembic과 런타임 권한을 적용한 뒤 앱·Cognito 설정을 갱신한다. v1.0→v1.1의 테이블·scope 이름 변경은 전환 구간에 API와 인증 설정이 일치하도록 유지보수 창에서 수행한다. 신규 field·archive 등록은 HTTPS와 저장 경로가 준비된 설치부터 활성화한다.

코드 롤백 시 추가된 DB 스키마와 사본·전송 기록은 유지한다. 복제 이력이 있는 DB의 파괴적인 downgrade는 거부한다. 기존 v1.0으로 돌아가야 하는 경우 v1.1 명칭 마이그레이션과 인증·API까지 별도 전환 계획으로 복구한다. 운영 `main` 승격은 해당 릴리스의 명시적 승인과 PR을 따른다.

## NAS 컨테이너 실행

NAS가 Docker Compose와 대상 CPU의 Python 3.12 이미지를 지원하면 저장소 루트에서 아래처럼 이미지를 빌드한다. `deploy/edge/Dockerfile`은 잠금 파일의 해시로 의존성을 설치하고 기본 UID/GID 10001로 실행한다. 실제 NAS의 계정·공유 폴더 권한에 맞춰 빌드 인자와 디렉터리 소유권을 조정한다. 클라이언트 비밀은 이 실행 계정만 읽게 한다.

```sh
docker build -f deploy/edge/Dockerfile -t dsc-edge-server:1.2.0 .
export DSC_OFFICE_CONFIG_DIR=/absolute/config/dsc
export DSC_OFFICE_STATE_DIR=/absolute/state/dsc-office
export DSC_OFFICE_NAS_ROOT=/absolute/nas/dsc-archive
docker compose -f deploy/edge/compose.office.yaml up -d
```

설정 디렉터리의 office.env와 비밀 파일, 상태 디렉터리와 실제 NAS 루트를 bind mount한다. 두 프로세스는 SQLite·파일 잠금과 NAS 경로를 공유한다. 수신기 포트는 호스트 루프백으로만 공개하며 호스트의 HTTPS 역방향 프록시를 연결한다. 실제 NAS OS·CPU·파일시스템 지원과 재시작·정전 동작은 장비 도착 후 검수한다.
