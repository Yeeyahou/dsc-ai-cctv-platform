# 외부 AAS 소비자 목업 실행 안내

파트너 관점에서 기존 백엔드의 AAS 읽기 기능과 데이터 권한을 확인하는 HTTP 클라이언트다. 별도 AAS Lambda나 DB를 만들지 않는다. 이 도구의 성공은 설정한 URL의 AAS 읽기 부분집합에 대한 검증이며 IDTA 전체 서비스 프로파일 인증이나 실제 파트너 제품의 검수 완료를 뜻하지 않는다.

## 개발 계정 준비

`examples/aas-consumer.json`을 `runtime/aas-consumer.json`으로 복사하고 개발 API·Cognito URL, 파트너 app client ID와 자산 식별자를 입력한다. app client에는 client credentials와 dsc/aas.read scope가 필요하고 백엔드 등록 주체에도 같은 scope 및 읽을 자산·Submodel 권한이 있어야 한다. 실제 장기 AWS 키는 필요 없다.

client_secret_file에는 비밀의 파일 경로만 넣는다. 비밀 파일은 0600으로 만들고 Git에 넣지 않는다. 직원 access token을 사용할 때는 token_url·client_id·client_secret_file을 제거하고 access_token_file만 설정한다. 도구는 ID token 대신 access token을 요구한다. 환경 변수나 명령 인자로 토큰을 직접 전달하지 않는다. 인증 실패·토큰 만료는 결과를 FAIL로 기록하며 무한 재시도하지 않는다. 새 토큰을 확보하고 새 보고서 이름으로 다시 실행한다.

허용 aas_id와 global_asset_id는 같은 등록 자산이어야 한다. submodel_ids는 이 주체에 허용된 정확한 목록이다. 예제 파트너는 Nameplate·TechnicalData만 읽고 TimeSeries는 금지한다. TimeSeries가 허용되면 dsc/timeseries.read와 해당 Submodel 등록 권한도 필요하다. forbidden_aas_id는 실제 존재하지만 해당 주체에 허용되지 않은 자산이어야 한다. forbidden_submodel_id도 실제 등록된 금지 Submodel이어야 한다. 없는 ID로 시험하면 권한 검수가 성립하지 않는다. 등록 사실은 관리자 측에서 별도 확인한다.

## 실행과 결과

```sh
PYTHONPATH=src python -m dsc.devtools.aas_consumer \
  --config runtime/aas-consumer.json \
  --report runtime/aas-result-run1.json
```

호출 순서는 인증, description, globalAssetId Discovery, Registry 목록·단건, Shell·Submodel 참조, Submodel descriptor·본문·$value, 금지 자산·Submodel의 404 확인이다. 목록은 페이지를 끝까지 읽고 반복 cursor와 최대 페이지 수 초과를 거부한다. Shell·Submodel은 저장소의 고정 IDTA 3.1.2 스키마로 검사한다. 반환 ID·globalAssetId·Submodel 목록을 설정과 대조한다. $value는 객체 직렬화와 접근 성공을 검사하며 실제 센서 값의 의미·정확도는 별도 검수한다.

정상은 종료 코드 0과 PASS, 실패는 1과 FAIL이다. 결과에는 경로 종류·HTTP 상태·검사 수·시각만 기록하며 토큰, 응답 자산 데이터, 서명 URL, 예외 원문은 저장하지 않는다. 보고서는 기존 파일을 덮어쓰지 않으므로 실행별 새 이름을 쓴다. 기본 HTTPS이며 테스트에 한해 allow_local_http로 127.0.0.1 HTTP를 명시적으로 허용할 수 있다. 프록시 환경 변수와 리다이렉트를 따르지 않으며 descriptor의 외부 href에 토큰을 보내지 않는다. 요청은 15초, 연결은 5초 제한이며 응답은 2 MiB까지 읽는다.

## 검증 경계

`pytest tests/test_aas_consumer.py`는 실제 API·권한·AAS 직렬화 코드를 테스트 전용 HTTP transport로 호출한다. Cognito 및 외부 네트워크는 격리한다. 실제 개발 환경의 Cognito 토큰과 등록 자산으로 위 CLI를 실행한 보고서, 파트너 제품과의 연동 확인은 별도로 남긴다. 퇴사자 비활성·다른 센터·이벤트·클립에 대한 전체 권한 회귀는 기존 백엔드 시험과 AT06·AT07에서 함께 확인한다.
