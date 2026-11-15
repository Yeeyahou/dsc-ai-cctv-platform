# 장애 재현 도구와 무결성 확인 안내

실제 장비가 오기 전에 API 연결의 지연·요청 유실·응답 유실을 반복할 수 있도록 loopback 전용 장애 프록시를 제공한다. 자동 시험에는 별도 전송 프로세스의 SIGKILL 복구도 포함한다. 사무실 NAS의 실제 정전·파일시스템 영속성과 인터넷 전체 장애는 이 시험으로 완료 처리하지 않는다.

## 자동 시험

```sh
pytest tests/test_process_rehearsal.py tests/test_replication.py
```

process_rehearsal은 실제 127.0.0.1 TCP 소켓과 제품 EdgeClient·inbox·SQLite outbox를 사용한다. 백엔드는 실제 업무 코드와 임시 DB를 사용하고 테스트 전용 Cognito·S3·SQS 대역을 사용한다. OS 송신 프로세스가 실제 백엔드 커밋 뒤 ACK를 받기 전에 종료되면 outbox는 SENDING으로 남는다. 재시작 시 같은 ID·본문으로 보내 200 duplicate를 받고 ACKED가 되는지 확인한다. AAS CLI도 별도 프로세스에서 실제 loopback 소켓으로 호출한다. 물리 전원 차단과 AWS 서명 검증 시험은 별개다.

replication 회귀 시험은 부분 파일·크기 초과·해시 불일치, NAS ACK 유실, 파일 확정과 manifest·서버 보고 사이 종료, 사무실 워커·NAS·회선 비정상 모의, 현재 lease 보호, 오래된 완료·revision·권한 회수, 미확인 사본 보존과 30일 정리 경쟁을 포함한다. PostgreSQL 전용 동시성 항목까지 실행하려면 전용 임시 DB를 지정한다. 실제 NAS 대신 임시 디렉터리와 제어 가능한 시계를 쓴다.

## 개발 API에서 직접 재현

먼저 개발 전용 자산·엣지 주체와 합성 영상용 환경을 준비한다. 프록시는 API 제어 JSON만 전달하며 요청 최대 256 KiB, 응답 최대 4 MiB다. S3 영상 바이트 업로드 경로와 NAS HTTPS 수신 경로를 이 프록시에 통과시키지 않는다. 포트는 127.0.0.1에만 열리며 고정한 upstream으로만 전달한다.

```sh
PYTHONPATH=src python -m dsc.devtools.fault_proxy \
  --upstream https://example.execute-api.ap-northeast-2.amazonaws.com/dev \
  --port 18080 --mode drop-after --route-prefix /ingest --nth 1
```

개발용 엣지 환경 파일의 `DSC_EDGE_ENVIRONMENT`를 test, `DSC_EDGE_API_URL`을 `http://127.0.0.1:18080`으로 지정한다. `DSC_EDGE_TOKEN_URL`은 실제 개발 Cognito HTTPS를 유지한다. 운영 설정 파일과 상태 디렉터리는 사용하지 않는다. 기존 엣지 서버를 중지하거나 운영 상태를 지우지 말고 별도 등록 주체·inbox·state_dir로 실행한다. 프록시는 인증·인가를 우회하지 않고 원래 토큰을 개발 API로 전달한다. 기본 TLS 인증서 검증을 유지하고 HTTP 리다이렉트를 따르지 않는다.

drop-before는 선택한 요청을 upstream에 보내기 전에 연결을 끊는다. drop-after는 upstream 처리와 응답 수신 후 클라이언트 연결을 끊어 저장 후 ACK 유실을 재현한다. delay는 선택한 요청을 보내기 전에 delay-seconds만큼 기다린다. nth는 route-prefix와 일치한 요청 중 1부터 세는 순서이며 한 실행에서 해당 요청 한 번에만 장애를 넣는다. 다시 재현하려면 프록시를 재시작한다.

```sh
PYTHONPATH=src python -m dsc.devtools.fault_proxy \
  --upstream https://example.execute-api.ap-northeast-2.amazonaws.com/dev \
  --mode delay --delay-seconds 40 --route-prefix /ingest
```

엣지의 기본 API timeout 30초보다 지연을 길게 하면 엣지는 재시도할 수 있다. 늦은 최초 요청과 재시도가 겹쳐도 같은 이벤트가 한 건만 남아야 한다. 엣지 재시도는 backoff를 사용하므로 즉시 ACKED가 되지 않을 수 있다. Ctrl-C로 프록시를 종료하고 시험 전후 개발 API·등록값·이벤트 ID·관측 시각을 보고서에 남긴다. 토큰·서명 URL·lease 토큰 전체를 기록하지 않는다.

## 무결성 확인 순서

목업 plan의 eventId·clipId·revision·크기·SHA-256을 기준으로 한다. outbox의 최초 본문이 같은지, 중복 이벤트가 없는지, NAS 완료가 실제 파일 확정 뒤인지, 실패 시 현장 사본이 남는지 확인한다. 업로드는 현재 lease의 시도 객체만 READY가 되어야 하고 이전 완료 통지는 거부돼야 한다. 다운로드한 바이트의 크기와 SHA-256을 원본과 대조한다. ETag는 SHA-256으로 취급하지 않는다.

사무실 수신기·업로더 중단, NAS 마운트 해제·잘못된 볼륨, 사무실 회선 차단, 현장 회선 차단, 양쪽 동시 장애는 실제 설치 후 별도로 수행한다. 업무 데이터가 없는 전용 검수 디렉터리에서 담당자와 중단 범위를 정한 뒤 실행하고 운영 영상이나 NAS 볼륨을 파괴하지 않는다. 동시 장애 및 현장 보관 기간 밖 클립은 복구 가능한 사본 자체가 없을 수 있다. 미전송·미확인 데이터를 지워 용량을 맞추는 방식은 사용하지 않는다.
