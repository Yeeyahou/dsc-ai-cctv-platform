# AWS 개발 환경 배포

## 대상과 현재 상태

개발 환경은 `develop`에서 검증한 코드를 서울 리전 `ap-northeast-2`에 배포한다. 자원 이름은 `dsc-dev-` 접두사를 사용하고 기존 회사 자원과 분리한다. 개발 배포는 운영 승인이나 `main` 승격을 의미하지 않는다.

현재 배포 계정의 `developers` 그룹에는 `PowerUserAccess`, `IAMReadOnlyAccess`가 연결돼 있다. 서비스 자원 조회는 가능하지만 배포에 필요한 IAM 역할 생성·정책 등록·역할 전달 권한은 없다. 이 권한이 준비되기 전에는 과금 자원을 생성하지 않는다.

2026년 11월 8일 사전 점검에서 `iam:CreateRole`과 `iam:PassRole`은 모두 `implicitDeny`로 확인됐다. 아래 추가 정책은 IAM Access Analyzer 검사에서 지적 사항이 없었고, 템플릿은 cfn-lint와 프로젝트 cfn-guard 검사를 통과했다. 이는 실제 배포 성공을 뜻하지 않는다. 스택 생성·DB 초기화·실제 API 연동 시험은 아직 수행하지 않았다.

계정에 지속 보관용 CloudTrail trail이 없는 것도 확인했다. 현재 관리 이벤트 감사는 기본 이벤트 기록 범위에 한정되며, 별도 보관 체계는 관리자와 결정해야 한다.

## 관리자가 추가할 배포 권한

[배포 역할 관리 정책](../deploy/iam/dsc-dev-deployer-policy.json)을 검토한 뒤 배포 사용자에게 추가한다. 이 파일은 계정 `955268448422`에 한정한 관리자 검토용 정책이며 코드에서 자동 적용하지 않는다.

관리자 권한으로 실행하는 예:

```sh
aws iam put-user-policy \
  --user-name jungwon_dev \
  --policy-name DscDevDeploymentRoles \
  --policy-document file://deploy/iam/dsc-dev-deployer-policy.json
```

정책은 다음 범위만 추가한다.

- 이름이 `dsc-dev-`로 시작하는 역할의 생성·정리와 역할 정책 관리
- Lambda, API Gateway, Scheduler에 해당 역할을 전달하는 권한
- 필요한 런타임 관리 정책의 제한된 연결

새 역할에는 `PowerUserAccess`를 **permissions boundary**로 반드시 설정한다. 이는 역할에 PowerUser 권한을 부여하는 것이 아니라, 역할의 개별 정책이 줄 수 있는 권한의 상한을 제한하는 장치다. 실제 런타임 권한은 각 역할의 서비스별 정책으로 제한한다. 배포 사용자는 이 상한을 제거하거나 다른 값으로 바꿀 수 없다. 사용자·그룹 정책 변경과 관리자 역할 전달 권한은 추가하지 않는다.

앱 스택의 `PermissionsBoundaryArn` 파라미터에는 `arn:aws:iam::aws:policy/PowerUserAccess`를 전달한다. Lambda API·유지관리, API Gateway 로그, Scheduler 실행 역할에 같은 상한을 적용한다. 다른 환경에서는 이 파라미터를 생략해 기존 동작을 유지할 수 있다.

`dsc-dev-` 역할 접두사는 이 프로젝트용으로 예약한다. 운영 또는 타 프로젝트 역할에 이 접두사를 사용하지 않는다. 관리자 검토 시 기존 SCP·permissions boundary·세션 정책도 함께 확인한다.

## 개발 구성과 비용 기준

개발용 기준 구성은 별도 VPC, 사설 DB, 소형 PostgreSQL 단일 AZ 인스턴스, NAT Gateway 한 개, 기존 API Gateway REST·Cognito·Lambda·WAF·SQS·S3다. 별도 도메인을 구입하지 않고 AWS 기본 HTTPS 주소로 우선 검증한다. 단일 AZ는 개발 환경의 명시적인 선택이며 운영용 DB의 Multi-AZ 기본값을 바꾸지 않는다.

이 구성은 배포 계획이다. 네트워크 생성, 기본 HTTPS 주소 지원 및 DB 초기화 절차는 추가 구현과 검증이 필요하다. 현재 변경은 관리자 검토용 권한 정책과 실행 역할의 권한 상한 설정까지다.

서울 리전 공식 Price List API 조회 기준, 월 730시간 사용 시 다음 기본 비용이 발생한다.

| 항목 | 기준 | 월 USD |
|---|---|---:|
| PostgreSQL `db.t4g.small` | 시간당 0.051 USD | 37.23 |
| DB gp3 저장소 | 30 GiB, GiB당 월 0.131 USD | 3.93 |
| NAT Gateway | 시간당 0.059 USD | 43.07 |
| NAT 공인 IPv4 | 시간당 0.005 USD | 3.65 |
| 합계 | WAF·요청·저장·전송·로그·비밀 관리·세금 제외 | 87.88 |

WAF 등 기본 서비스와 소량의 시험 사용을 합치면 월 약 100 USD 수준을 예상한다. 실제 요금 상한을 보장하는 수치는 아니며 트래픽·영상 용량·보관량에 따라 증가한다. NAT 데이터 처리비와 외부 전송비도 별도다. 요금 조회일은 2026년 11월 8일이며 크레딧·할인은 반영하지 않았다.

관리형 서비스의 표준 요금은 [RDS PostgreSQL 요금](https://aws.amazon.com/rds/postgresql/pricing/)과 [VPC 요금](https://aws.amazon.com/vpc/pricing/)에서 확인한다. 배포·연동 검증 결과는 실제 수행 후 이 문서에 기록한다.
