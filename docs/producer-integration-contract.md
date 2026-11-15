# 분석 프로그램과 현장 엣지 연동 계약

이 문서는 CCTV·로봇 분석 및 녹화 프로그램 담당자와 현장 엣지 담당자가 구현을 맞출 때 사용하는 전달용 계약이다. 기준은 연동 규격서 v1.2의 5장과 10장 및 `dsc.shared.contracts`다. 새 HTTP 입력 API를 추가하지 않는다. 담당자 간 실제 합의와 현장 검수는 아직 남아 있다.

## 책임과 경로

분석 프로그램은 이벤트 식별, 녹화 완료, 입력 파일 확정을 담당한다. 엣지는 로컬 트랜잭션 접수, outbox 재전송, 현장 관리 사본 확보와 사무실 복제를 담당한다. 사무실 워커는 NAS의 검증된 클립을 읽어 요청된 영상만 S3로 업로드한다.

CCTV와 로봇은 같은 형식을 사용한다. 각각 다른 inbox·클립 입력 루트·엣지 상태 디렉터리·등록 주체를 사용한다. `siteId`와 `assetId`는 서버에 등록된 값을 사용하며 본문의 siteId로 권한이 생기지 않는다. `localMedia.relativePath`는 해당 현장 `DSC_EDGE_NAS_ROOT` 아래 상대 경로다. 사무실 NAS 주소나 절대 경로를 넣지 않는다.

## 이벤트 파일

최종 이름은 `{eventId}.event.json`이다. `examples/inbox-event.json`이 기존 예제이며 JSON 최상위는 `kind: event`, `event`, 선택적인 `localMedia`다. `event`에는 schemaVersion 1.0, UUID eventId, 등록된 eventCode, siteId, 시간대가 있는 occurredAt, assets가 필요하다. 선택 필드는 생략하며 null을 보내지 않는다.

각 감지 이벤트마다 새 eventId를 생성한다. 재전송에서는 최초 eventId와 최초 본문을 그대로 유지한다. 변경된 본문을 같은 ID로 덮어쓰지 않는다. 발생·해소 쌍 DSC-102A/B·104A/B·106A/B·202A/B는 같은 incidentId, 서로 다른 eventId를 사용한다. 수신 순서가 바뀌어도 원본 시각과 incidentId를 유지한다. 지표 키와 허용 범위는 규격 5장 및 EventInput 모델을 따른다.

## 녹화 완료와 상태 갱신

녹화 중인 이벤트에는 같은 clipId와 RECORDING을 보낸다. 파일 쓰기를 완료하고 fsync·확정한 뒤 `{eventId}.{revision}.media.json`을 전달한다. 최상위는 `kind: media`, `eventId`, `media`, `localMedia`다. revision은 1부터 증가하며 같은 revision의 다른 내용을 재사용하지 않는다.

AVAILABLE에는 clipId, revision, startedAt, endedAt, contentType `video/mp4`, 실제 sizeBytes가 필요하다. endedAt은 startedAt보다 뒤여야 한다. localMedia에는 같은 clipId와 상대 경로를 넣는다. 확정한 영상 바이트와 경로는 수정하지 않는다. 다른 영상은 새 clipId로 식별한다. 파일 누락은 MISSING, 영상 비대상은 NOT_APPLICABLE을 사용한다. 이 문서는 영상 SHA-256을 이벤트 API의 새 필드로 추가하지 않는다. 엣지가 실제 파일을 읽어 복제·업로드용 크기와 해시를 계산한다.

## 원자적 전달과 접수 확인

생산자는 같은 파일시스템에 `.tmp`로 기록하고 파일을 fsync한 후 최종 JSON 이름으로 원자적으로 공개한다. 기존 이름에 다른 내용을 덮어쓰지 않는다. 디렉터리도 fsync한다. 엣지는 `.tmp`를 처리하지 않는다. 경로 이동(`..`), 절대 경로, 심볼릭 링크 우회는 허용하지 않는다.

inbox의 accepted 이동은 로컬 영속 접수이며 AWS 저장 완료를 뜻하지 않는다. AWS 저장 완료는 엣지 outbox의 ACKED로 확인한다. rejected와 `.reason`은 입력 수정이 필요한 상태다. 저장소 오류로 원본이 inbox에 남으면 원인을 조치하고 같은 입력으로 재시도한다. 현장 원본 삭제는 엣지의 별도 관리 사본 확보와 운영 보관 정책을 확인한 뒤 결정한다. 생산자가 accepted만 보고 엣지의 관리 사본까지 삭제하면 안 된다.

## 담당자와 확정할 항목

실제 등록 siteId·assetId, CCTV·로봇별 입력 경로와 OS 계정, JSON 공개 방식, 녹화 길이·코덱, 발생·해소 식별 지속성, 원본 정리 주체, 디스크 부족 알림과 로그 수집 위치를 확정한다. 계약 예제를 생산자와 엣지 양쪽 모델로 검증하고 실제 녹화 파일로 재시작·중복·순서 역전·파일 쓰기 중 종료를 검수한다. 현재 목업의 합성 영상은 모델 성능이나 실제 CCTV 코덱 호환성을 검증하지 않는다.
