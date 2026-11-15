# 분석 및 녹화 목업 실행 안내

실제 CCTV·로봇 없이 v1.2 inbox 입력을 만드는 개발 도구다. 이벤트 코드와 영상 색상·길이를 난수로 생성하며 감지 모델이나 실제 센서 연결은 수행하지 않는다. 랜덤 영상에는 PATH의 FFmpeg와 libx264가 필요하다. 합성 MP4가 이미 있으면 `--video`로 사용할 수 있으며 이 입력의 메타데이터는 1초로 취급하므로 1초짜리 영상을 사용한다.

## 생성과 실행

저장소 루트에서 가상 환경을 활성화한다. 아래 경로는 개발용이며 운영 inbox를 지정하면 실제 이벤트가 전송될 수 있다. 로컬 생성 자체에는 AWS 연결이나 자격 증명이 필요 없다.

```sh
PYTHONPATH=src python -m dsc.devtools.producer \
  --run-dir runtime/mock/cctv-run1 \
  --inbox runtime/mock/cctv/inbox \
  --clip-root runtime/mock/cctv/input \
  --channel cctv --site-id site-1 --asset-id camera-1 \
  --count 20 --seed 42 --interval 0.2
```

로봇은 별도 run-dir·inbox·clip-root와 `--channel robot --asset-id robot-1`을 사용한다. 실제 엣지 설정의 `DSC_EDGE_INBOX`와 `DSC_EDGE_NAS_ROOT`를 각각 inbox와 clip-root에 맞춘다. serverId·인증·상태 디렉터리도 설치별로 분리한다. CCTV는 DSC-1 계열, 로봇은 DSC-2 계열에서 생성한다. DSC-3 온도 센서 입력은 이 영상 목업 대상 밖이다. 발생·해소 쌍은 순서대로 생성하고 같은 incidentId를 공유한다. count는 이벤트 수이며 각 이벤트에 RECORDING과 AVAILABLE revision 1을 전달한다.

영상은 160×120, 10 fps, 1~3초 H.264 MP4다. 먼저 전체 입력과 영상 및 해시를 run-dir에 확정한 뒤 clip-root와 inbox로 공개한다. seed는 코드·색상·길이 선택을 반복할 수 있게 하며 새 실행은 새 runId와 UUID를 만든다. 완전히 같은 ID·본문·바이트 재전송에는 아래 replay를 사용한다. 발생 시각을 고정하려면 `--started-at 2026-11-15T00:00:00+00:00`을 지정한다.

## 같은 입력 재전송

```sh
PYTHONPATH=src python -m dsc.devtools.producer \
  --run-dir runtime/mock/cctv-run1 \
  --inbox runtime/mock/cctv/inbox \
  --clip-root runtime/mock/cctv/input --replay
```

plan.json에는 eventId·clipId·코드·경로·크기·SHA-256과 JSON 해시가 남는다. 재전송 전 저장된 파일을 검사하고 바뀌었으면 중단한다. 같은 목적지 이름의 다른 바이트도 덮어쓰지 않는다. 실행 중단 후 plan.json이 있으면 replay한다. plan이 완성되기 전에 중단했다면 해당 디렉터리를 보존하고 새 run-dir로 생성한다. 이 도구는 운영 원본이나 실패한 실행을 자동 삭제하지 않는다.

## 확인과 한계

stdout의 events·envelopes는 파일 공개 수다. accepted는 로컬 접수, ACKED는 백엔드 저장 완료다. 엣지의 `--inspect-outbox`, `--inspect-copies`와 rejected 사유를 확인한다. 재실행 후 기존 이벤트 개수가 늘지 않는지, 클립 관리 사본의 크기·해시가 plan과 같은지 확인한다. `pytest tests/test_producer.py`는 실제 inbox·SQLite·관리 사본 경계를 검증하며 코덱 재생 시험은 별도로 수행한다. 합성 영상만 개발 환경에 올리고 실제 고객 영상은 사용하지 않는다.
