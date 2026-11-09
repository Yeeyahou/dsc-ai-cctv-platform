# AWS 개발 환경 배포

이 절차는 전용 개발 환경을 처음 설치할 때 사용한다. `develop`에서 검증한 소스를 서울 리전에 배포하고 회사 공용 VPC·DB나 운영 스택을 변경하지 않는다. 애플리케이션 스택 이름은 `dsc-dev-app`이다.

v1.0 개발 스택을 갱신할 때는 [엣지 서버 v1.1 전환 절차](edge-server-upgrade-v1.1.md)에 따라 먼저 DB를 마이그레이션하고 큐와 인증 설정을 함께 변경한다.

## 먼저 확인할 항목

- 회사가 부여한 `DevBoundary`와 `app-*` 역할 규칙을 사용한다. 별도의 개발자 IAM 정책을 추가하지 않는다. API, 유지관리, API Gateway 로그, 임시 DB 초기화 역할 이름과 정책 범위를 개발 스택에 맞춘다.
- 합성 데이터만 쓰는 개발 시험은 `ClipAuditMode=synthetic`을 지정한다. 이 모드는 개발 환경에서만 허용되며 S3 서버 액세스 로그를 유지한다. CloudTrail S3 데이터 이벤트는 수집하지 않으므로 실제 CCTV·고객 데이터를 사용하지 않는다.
- 실제 데이터를 사용하려면 회사 감사 기준을 확인한다. 외부 관리 trail을 쓸 때는 관리자가 `arn:aws:s3:::dsc-dev-app-ACCOUNT_ID-ap-northeast-2-clips/`의 객체 읽기와 쓰기를 기록하게 설정한다. 활성 상태와 범위를 확인한 뒤 `ClipAuditMode=external`로 배포한다. 운영 기본값인 `managed`는 기존 스택 관리형 데이터 trail을 유지한다.
- API Gateway의 CloudWatch 역할은 리전 공용 설정이다. 이미 연결된 역할이 있다면 이 템플릿으로 덮어쓰지 말고 관리자와 기존 설정 재사용을 먼저 조율한다.
- 개발 구성은 소형 단일 AZ PostgreSQL과 NAT Gateway 한 개를 사용한다. 두 서비스는 요청이 없어도 과금된다. 이전 서울 리전 조회 기준 약 월 100 USD 수준이며, 사용량과 세금은 별도다. 운영 DB의 Multi-AZ 기본값은 유지한다.
- 현재 서울 리전 계정의 Lambda 동시 실행 한도는 10이다. 개발 함수는 예약 동시 실행을 지정하지 않고 계정 한도를 공유한다. 운영 환경의 함수별 예약 설정은 유지한다.
- 실제 영상을 넣기 전에 개발 데이터만으로 검증한다. NAS를 열거나 AWS 액세스 키를 현장 PC에 설치하지 않는다.

## 배포 전 확인과 패키지 준비

Python 3.12 개발 환경과 AWS CLI를 사용한다. 다음 변수는 이 배포에만 적용한다.

```sh
export AWS_PROFILE=futurex
export AWS_REGION=ap-northeast-2
DSC_DEV_ACCOUNT=ACCOUNT_ID
DSC_DEV_BOUNDARY="arn:aws:iam::${DSC_DEV_ACCOUNT}:policy/DevBoundary"

python scripts/check_dev_deployment.py --profile "$AWS_PROFILE" --account "$DSC_DEV_ACCOUNT" \
  --region "$AWS_REGION" --synthetic
aws apigateway get-account --query cloudwatchRoleArn --output json
python scripts/build_dev_artifacts.py build/dev-release
cfn-lint infra/*.yaml
cfn-guard validate --data infra/ --rules infra/security.guard
```

검사 스크립트는 역할 정책을 시뮬레이션하며 AWS 자원을 생성하지 않는다. `external` 모드로 배포할 때는 `--synthetic` 대신 `--trail-arn ADMIN_TRAIL_ARN`을 넘겨 해당 trail을 검증한다. 필요한 검사가 실패하면 배포를 시작하지 않는다. 빌드 경로는 비어 있는 새 경로여야 하며, 생성된 ZIP에는 환경 설정·자격 증명·영상 파일을 넣지 않는다.

## 네트워크와 데이터베이스

1. `infra/dev-artifacts.yaml`을 `dsc-dev-artifacts` 스택으로 배포한다. `BucketName` 출력에 ZIP을 올린다. 기존 회사 배포 버킷을 임의로 변경하지 않는다.
2. `infra/dev-network.yaml`을 `dsc-dev-network`로 배포한다. 출력에서 `VpcId`와 쉼표로 구분한 `PrivateSubnetIds`를 읽는다.
3. `infra/database.yaml`을 `dsc-dev-db`로 배포한다. 앞에서 읽은 VPC와 서브넷을 전달하고, `MultiAZ=false GenerateDevRuntimeSecret=true`를 지정한다. 출력에서 `DatabaseSecurityGroupId`, `MasterSecretArn`, `DevRuntimeSecretArn`을 읽는다. DB 인스턴스는 비공개·암호화·TLS 강제·삭제 보호를 유지한다.
4. `db-init.zip`은 해시가 포함된 객체 키로 배포 버킷에 올린다. `infra/dev-db-init.yaml`을 `dsc-dev-app-db-init` 스택으로 배포한다. VPC, 서브넷, DB 보안 그룹, 마스터·런타임 secret ARN, `PermissionsBoundaryArn`, `ArtifactBucket`, `ArtifactKey`를 지정한다. 명명된 실행 역할이 있으므로 `CAPABILITY_NAMED_IAM`을 사용한다.
5. IAM으로 초기화 함수를 한 번 호출한다. 응답의 `FunctionError`가 없고 결과가 `{"status":"initialized"}`인지 확인한다.

```sh
aws lambda invoke --function-name dsc-dev-app-db-init \
  --cli-binary-format raw-in-base64-out --payload '{}' build/dev-release/init-result.json
```

초기화 함수는 사설 DB에 접속해 Alembic 스키마와 런타임 계정 권한을 설정한다. 마스터 자격 증명은 함수 밖으로 꺼내지 않는다.

## 앱 배포와 검증

```sh
aws cloudformation package --template-file build/dev-release/app-local.yaml \
  --s3-bucket "$DSC_DEV_ARTIFACT_BUCKET" --s3-prefix dsc-dev-app \
  --output-template-file build/dev-release/app-packaged.yaml
```

패키지 템플릿으로 `dsc-dev-app` 변경 세트를 만든다. 필수 파라미터는 `Environment=dev`, 선택한 `ClipAuditMode`(합성 시험은 `synthetic`), `PermissionsBoundaryArn`, VPC·서브넷·DB 보안 그룹, `DatabaseSecretArn`(마스터가 아닌 런타임 secret), 고유한 `CognitoDomainPrefix`, 개발용 HTTPS 로그인 콜백 및 로그아웃 주소다. 별도 도메인이 없으면 `DomainName`, `CertificateArn`, `HostedZoneId`를 생략한다. `CAPABILITY_NAMED_IAM`을 지정한다. 변경 세트의 자원과 CloudFormation 사전 검사 결과를 확인한 후 실행한다.

도메인 없는 개발 환경은 첫 스택 생성 이후에 API ID가 정해진다. `ApiUrl` 출력을 같은 스택의 `DevPublicBaseUrl`에 전달해 한 번 갱신한다. 갱신하기 전에는 AAS 응답의 기준 링크가 시험용 주소이므로 고객이나 현장 엣지 서버에 연결하지 않는다.

스택 출력의 Cognito client ID·큐 URL과 실제 테스트 주체 ID를 [등록 예제](../examples/registration.json)에 반영한다. 초기화 함수에 `{"registration": {...}}` 형태로 호출해 등록하고, 인증 실패 응답과 등록된 주체의 이벤트 수신·조회·AAS 조회를 확인한다. 클립 검증은 합성 MP4로 작업 생성부터 S3 임시 링크까지 확인한다. `external` 모드에서는 관리자 trail과 S3 접근 로그에서 해당 접근을 대조한다. 함수 사용이 끝나면 **초기화 함수 스택만** 삭제해 마스터 secret에 접근하는 실행 경로를 없앤다. DB와 앱 스택은 유지한다.

권한·감사 검사나 변경 세트에서 거부되면 오류가 난 자원과 작업을 확인한다. 거부된 회사 정책을 우회하거나 운영 리소스를 변경하지 않는다.
