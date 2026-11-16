# 맥북 시험과 현장 PC용 Docker 실행 안내

맥북에서 확인한 엣지 실행 환경을 다른 현장 PC에서도 재현하기 위한 구성이다. 기존 저장소와 공통 Docker 이미지를 사용하며, CCTV와 로봇은 각각 별도 설정·입력·상태 폴더로 실행한다.

## 무엇이 실행되는가

| 구성 | 역할 | 현재 확인 범위 |
| --- | --- | --- |
| `deploy/edge/compose.field-local.yaml` | 네트워크를 차단한 개발용 파일 수신기 | 실제 MP4와 목업 이벤트 접수, DB·관리 사본 보존 |
| `deploy/edge/compose.field.yaml` | 기존 `dsc-edge-server`를 실행하는 현장용 구성 | Docker 설정 검사; 실제 AWS 인증·NAS 복제는 설치 정보 준비 후 검수 |

시험용 수신기는 기존 엣지의 inbox·SQLite·클립 보관·복구 코드를 사용한다. 토큰이나 AWS 클라이언트를 만들지 않으며 Docker의 `network_mode: none`으로 외부 통신을 차단한다. 운영 엣지의 인증을 우회하거나 운영용 오프라인 모드를 추가한 것이 아니다.

카메라 영상 수신·분석·클립 생성은 별도 분석·녹화 프로그램의 역할이다. VLC는 카메라 영상과 녹화 파일을 확인할 때 사용한 도구이며 이 Docker 이미지에 포함하지 않는다. 이번 구성은 실시간 사무실 스트리밍 기능을 추가하지 않는다.

## 맥북에서 같은 시험 실행

Docker Desktop을 실행하고 저장소 루트에서 다음 명령을 사용한다. 호스트에는 Python 3.12 이상과 Docker Compose가 필요하며 호스트의 Python 패키지 설치는 필요 없다. 실제 녹화가 끝난 MP4와 해당 파일의 길이를 지정한다. 아래 25.174169초는 이번 현장 녹화 예시의 길이이므로 다른 영상이면 실제 값으로 바꾼다.

```sh
python3 scripts/check_field_docker.py \
  --video /absolute/path/to/camera-sample.mp4 \
  --duration-seconds 25.174169
```

실행할 때마다 새 Compose 프로젝트와 `runtime/field-docker/run-*` 폴더를 만든다. 기존 실행이나 다른 Docker 서비스를 변경하지 않는다. 이미지 빌드가 끝난 상태라면 `--no-build`로 빌드를 생략할 수 있다.

스크립트는 다음을 실제 Docker 엔진에서 확인한다.

1. 일반 사용자·읽기 전용 컨테이너와 외부 네트워크 차단.
2. 원본 MP4를 시험 입력으로 복사하고 이벤트·녹화 완료 JSON을 원자적으로 공개.
3. 기존 엣지가 접수한 outbox 2건과 클립 관리 사본 1개 확인.
4. 실제 녹화 원본과 관리 사본의 크기·SHA-256 일치.
5. 컨테이너 재시작과 컨테이너 삭제·재생성 후 DB·영상 보존.
6. 같은 ID·본문 재입력 시 중복 방지, 작성 중 `.tmp` 파일 보존.
7. 백엔드에 보내지 않은 데이터가 `PENDING`으로 유지되고 원본 영상이 변경되지 않음.

이벤트는 `local-test-site`의 목업 `DSC-101`이며 실제 감지 결과가 아니다. 이벤트 시각은 시험 시각을 사용한다. 클립 길이는 전달한 값을 사용하고 크기와 SHA-256은 파일에서 직접 계산한다. 새 영상의 길이는 VLC의 미디어 정보 등으로 확인해 입력한다.

시험 후 수신기는 계속 실행되어 파일 입력을 기다린다. 실행 결과에 표시된 `USAGE.md`에는 해당 프로젝트의 상태 확인·로그·중지·다시 시작 명령이 정확한 경로와 함께 들어 있다. 새 시험을 여러 번 실행하면 프로젝트도 여러 개 생기므로 사용하지 않는 실행은 해당 `USAGE.md`의 명령으로 중지한다.

## 맥북에 보존되는 파일

| 호스트의 시험 폴더 내부 | 용도 |
| --- | --- |
| `compose.env` | 프로젝트 이름·맥북 UID/GID·영속 폴더 경로 |
| `producer/` | 목업 이벤트와 녹화 완료 JSON 원본 |
| `inbox/` | 입력 대기·접수·격리 파일 |
| `input-clips/` | 분석·녹화 프로그램이 제공하는 클립을 대신한 시험 입력 |
| `state/outbox.sqlite` | 미전송 이벤트와 클립 사본 정보 |
| `managed-clips/` | 기존 엣지가 확보한 클립 관리 사본 |
| `result.json`, `before.json`, `after.json` | 시험 결과와 재시작 전후 상태 |
| `docker.log`, `USAGE.md` | 명령 로그와 해당 프로젝트 사용법 |

호스트 폴더를 컨테이너에 bind mount하여 컨테이너의 수명과 데이터를 분리한다. 컨테이너를 제거해도 호스트 폴더는 남지만 호스트 디스크 장애·폴더 삭제까지 보호하지는 않는다. 이 동작과 호스트 경로 의존성은 [Docker bind mount 문서](https://docs.docker.com/engine/storage/bind-mounts/)를 따른다. 실제 영상·로컬 DB·환경 파일·시험 결과는 `runtime/` 아래에 두어 Git에서 제외한다.

## 실제 현장 PC에서 운영 엣지 실행

운영용은 `deploy/edge/compose.field.yaml`과 같은 폴더의 `field-compose.env.example`, `field.env.example`을 사용한다. 기존 API·DB·인증·NAS 복제 계약은 바꾸지 않는다.

1. 현장 PC에 Docker Engine/Compose를 준비한다. Linux 서비스 계정과 실행 UID/GID를 정하고 폴더·비밀 파일 소유권을 일치시킨다. 예제 기본값은 10001:10001이다. macOS에서는 실제 사용할 계정의 `id -u`, `id -g` 값을 사용한다.
2. 설정, inbox, 생산자 클립, 상태 폴더를 사전에 만든다. Compose의 `create_host_path: false`로 잘못된 경로를 새 빈 폴더로 조용히 대체하지 않도록 했다.
3. `field-compose.env.example`을 로컬 `compose.env`로 복사해 실제 호스트 절대 경로와 UID/GID를 넣는다.
4. `field.env.example`을 설정 폴더의 `field.env`로 복사한다. 등록된 엣지·센터 식별자, 실제 API·Cognito 주소, 설치별 client ID와 사무실 수신기 허용 호스트를 넣는다. `client-secret`은 별도 로컬 파일에 저장하고 컨테이너 실행 계정만 읽을 수 있도록 소유권과 `0600` 권한을 적용한다.
5. 분석·녹화 프로그램의 출력 폴더를 아래 매핑에 맞춘 뒤 실행한다. 입력 JSON과 클립 규약은 [분석·녹화 연동 계약](producer-integration-contract.md)을 따른다.

| 호스트 환경 변수 | 컨테이너 내부 경로 | 접근 |
| --- | --- | --- |
| `DSC_FIELD_CONFIG_DIR` | `/etc/dsc` | 읽기 전용 |
| `DSC_FIELD_INBOX_DIR` | `/var/spool/dsc/inbox` | 접수·격리 이동을 위해 읽기/쓰기 |
| `DSC_FIELD_INPUT_DIR` | `/var/spool/dsc/input-clips` | 읽기 전용 |
| `DSC_FIELD_STATE_DIR` | `/var/lib/dsc/field` | DB와 `clips/` 관리 사본 보존 |

```sh
docker compose --env-file /absolute/path/to/compose.env \
  -p dsc-cctv-field -f deploy/edge/compose.field.yaml up --build -d

docker compose --env-file /absolute/path/to/compose.env \
  -p dsc-cctv-field -f deploy/edge/compose.field.yaml logs --tail=50 field
```

실제 API 주소·설치 인증·등록 권한·사무실 수신 주소가 준비되어야 클라우드 전송과 사무실 복제가 가능하다. 현재 예제의 placeholder 값을 그대로 사용한 실행을 배포 완료로 보지 않는다. 운영용 Compose는 외부 포트를 공개하지 않으며 API·S3·사무실 수신기로 아웃바운드 연결을 사용한다.

CCTV와 로봇은 다른 프로젝트 이름과 모든 호스트 폴더·식별자·자격 증명을 사용한다. 동일한 물리 클립·미전송 DB를 서로 다른 등록 주체의 데이터로 재사용하지 않는다. 이번 `local-test-site` 시험 DB는 운영 DB로 옮기지 않고 새 운영 설치를 준비한다.

## 이동·업데이트와 남은 검수

실제 설치를 교체할 때는 기존 엣지를 정상 중지하고 inbox·생산자 클립·상태 폴더 전체를 보존한다. 상태 폴더에는 SQLite의 WAL 관련 파일과 관리 클립도 포함된다. 같은 논리 설치를 옮길 경우에만 기존 식별자·인증과 함께 복구하고, 기존 장비와 새 장비가 동시에 같은 outbox를 전송하지 않게 한다. 데이터 이관 뒤 크기·SHA-256·미전송 건수를 대조한다.

macOS의 절전·덮개 닫힘과 Docker Desktop 종료는 프로그램 실행에 영향을 준다. `restart: unless-stopped`는 Docker 엔진이 동작할 때의 재시작 정책이며 전원이 꺼진 PC를 대신하지 않는다. 최종 현장 OS·CPU에서 이미지 빌드·권한·부팅 후 실행을 검수한다.

이번 로컬 Docker 시험은 맥북의 Linux/ARM64 컨테이너에서 수행한다. 실제 현장 PC, 분석 프로그램, AWS 인증·전송, 물리 NAS·회선 장애 전환, 전원 차단과 장시간 부하 시험은 [검수 기록](acceptance-report-template.md)에 환경·결과를 나누어 기록한다. 소스와 설정을 Git에 보관하는 것과 이 실제 환경 검수는 별도다.
