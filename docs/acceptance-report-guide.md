# 검수 보고서 생성과 인수 안내

AT01~AT25의 자동 시험 증거를 같은 방식으로 수집하고 실제 장비 검수와 구분하기 위한 안내다. `docs/acceptance-matrix.json`은 각 항목의 기대 결과·시험 함수·추가 현장 검수를 연결한다. 생성된 PASS는 자동 시험의 범위이며 전체 인수 완료가 아니다.

## 기본 실행

```sh
PYTHONPATH=src python -m dsc.devtools.acceptance \
  --output runtime/acceptance/sqlite-run1
```

저장소 루트에서 가상 환경을 활성화한 뒤 실행한다. 기본 실행은 셸에 DSC_TEST_POSTGRES_URL이 있어도 제거하고 임시 SQLite로 시험한다. PostgreSQL 전용 8개 시험은 건너뛰므로 관련 AT는 INCOMPLETE, 도구 종료 코드는 1이 된다. pytest 자체가 실패했다는 뜻과 구분해 report.json의 exitCode 및 skipped를 확인한다.

## PostgreSQL 전체 실행

```sh
docker compose up -d --wait postgres
DSC_TEST_POSTGRES_URL='postgresql+psycopg://dsc_test:local-test-only@127.0.0.1:55432/dsc_test' \
  PYTHONPATH=src python -m dsc.devtools.acceptance \
  --postgres --output runtime/acceptance/postgres-run1
docker compose down
```

이 DB는 시험이 테이블을 삭제하고 다시 만드는 전용 임시 DB여야 한다. 개발·운영 데이터가 든 DB를 지정하지 않는다. PostgreSQL은 임시 저장소를 사용하고 종료하면 데이터가 사라진다. 도구는 --postgres와 명시적인 환경 변수가 함께 있어야 PostgreSQL을 사용한다. TCP 포트 바인딩을 막는 실행 샌드박스에서는 loopback 프로세스 시험 권한이 필요하다.

## 산출물과 판정

output은 새 디렉터리여야 한다. junit.xml은 각 pytest 항목의 결과, pytest.log는 원본 시험 출력, report.json은 커밋·추적 파일 변경 여부·Python·OS·DB 종류·AT 대응 결과, report.md는 사람이 읽는 보고서다. 보고서는 테스트 함수별 증거와 건너뜀·누락을 표시한다. 시험 이름 변경으로 대응이 끊기면 INCOMPLETE가 되며 대응표 자체도 자동 검사한다.

실패가 있으면 FAIL, 건너뜀·누락·수집 오류가 있으면 INCOMPLETE, 지정한 시험을 모두 실행해 통과했을 때만 localStatus PASS다. 실제 환경의 siteStatus는 자동으로 NOT_RUN을 기록한다. 모든 시험과 대응이 통과하면 종료 코드 0, pytest 실패는 해당 종료 코드, 대응 미완료는 1을 반환한다. 10분 제한을 넘으면 124와 미완료 기록을 남긴다. pytest·Ruff는 가상 환경에 설치돼 있어야 한다. Ruff는 별도로 `make lint`를 실행한다.

## 실제 환경 기록

`docs/acceptance-report-template.md`를 실행마다 복사한다. AWS 개발·물리 NAS·현장 PC·외부 파트너 각각의 버전과 등록값, 네트워크·보관 기간·일별 생성량·동시 작업 수를 적고 입력·기대·실제·증거·판정을 채운다. 기존 배포 기록은 과거 수행 사실로 연결하고 새 버전의 실제 검수로 자동 승계하지 않는다. 토큰·비밀·서명 URL·고객 영상은 첨부하지 않는다.

AT17·AT25는 30일 보관과 장애 누적량, 실제 NAS fsync·hard link 영속성, 전원·회선, 백업 복원과 RPO/RTO 측정이 핵심이다. 자동 시험 통과만으로 이 항목을 인수 완료로 바꾸면 안 된다. 문서의 템플릿을 채워 담당자가 결과와 한계를 검토한 뒤 최종 판정한다. main 승격과 운영 배포는 별도 릴리스 승인 절차를 따른다.
