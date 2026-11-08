# 브랜치 운영

## 브랜치 역할

| 브랜치 | 역할 | 변경 경로 |
|---|---|---|
| `main` | 검증·승인이 끝난 배포 기준 코드 | 같은 저장소의 `develop`에서 PR |
| `develop` | 기본 개발 기준, 기능 통합과 통합 검증 | 기능 브랜치에서 PR |
| `codex/feature/<작업명>` | 독립적인 기능 구현 | 최신 `origin/develop`에서 생성 |
| `codex/fix/<작업명>` | 오류 수정 | 최신 `origin/develop`에서 생성 |
| `codex/chore/<작업명>` 등 | 설정·문서·정리 작업 | 최신 `origin/develop`에서 생성 |

일반 흐름은 `develop → 기능 브랜치 → develop → main`이다. 기능 브랜치는 짧게 유지하고 하나의 목적을 다룬다. 기능 구현이 끝나도 통합 검증이나 배포 준비가 끝나지 않았다면 `develop`에 둔다. 별도의 상시 `release` 브랜치는 만들지 않는다.

## 기능 개발과 통합

작업 전 미커밋 파일을 확인하고 보존한다. 아래 명령은 작업을 시작할 수 있는 깨끗한 상태에서 실행한다.

```sh
git fetch origin
git switch -c codex/feature/event-search origin/develop
```

구현·검사·한글 컨벤션 커밋 후 작업 브랜치를 푸시하고 `develop` 대상으로 PR을 만든다. PR에는 문제와 변경 동작, 검증 결과, 배포·마이그레이션 영향을 적는다. 필요한 로컬 검사와 GitHub의 `test`, `branch-policy` 검사가 성공하고 미해결 대화를 처리한 뒤 병합한다.

기능 PR은 squash merge를 기본으로 한다. 병합 제목도 `feat(events): 이벤트 검색 조건 확장`처럼 한글 컨벤션을 따른다. 병합한 작업 브랜치는 재사용하지 않고, 다음 기능은 다시 최신 `origin/develop`에서 시작한다.

## main 승격

`develop → main` PR만 허용한다. 기능 브랜치에서 `main`으로 바로 보내거나 검증하지 않은 변경을 선택해 직접 커밋하지 않는다. 자동 검사 성공은 승격의 필요조건이다. 병합 담당자는 변경 범위에 맞춰 다음을 확인한다.

- 통합된 기능의 동작과 권한·복구 등 관련 회귀 검증
- API와 DB 변경의 호환성, 마이그레이션 및 복구 절차
- 배포 설정·의존성 변경과 필요한 AWS·현장 연동 검수
- 남은 문제와 운영 영향의 확인 및 해당 릴리스의 배포 승인

에이전트는 사용자가 해당 릴리스의 `main` 병합을 명시적으로 요청한 경우에만 승격한다. 단순한 구현·커밋·푸시 요청이나 CI 통과만으로 승격하지 않는다. `main` 병합과 실제 AWS·현장 배포는 별도 작업이다.

장기 브랜치의 공통 이력을 유지하기 위해 `develop → main`은 **merge commit**으로 병합한다. squash·rebase merge는 사용하지 않는다. 승격 후에는 `main → develop` 동기화 PR도 **merge commit**으로 반영한다. 최신 대상 브랜치 기준 검사가 필수이므로 이 동기화를 생략하면 다음 승격 시 기준 갱신이 필요할 수 있다. 운영 수정도 기본적으로 기능 브랜치에서 `develop`을 거친다.

## GitHub 보호 설정

기본 브랜치는 `develop`로 지정한다. `main`과 `develop`은 다음 설정으로 보호한다.

- 직접 푸시 대신 PR 사용 필수
- GitHub Actions의 `test`, `branch-policy` 성공 필수
- 병합 전에 최신 대상 브랜치를 반영한 검사 필수
- 미해결 PR 대화 해소 필수
- 관리자도 같은 보호 규칙 적용, 강제 푸시·브랜치 삭제 금지

별도 승인자 수는 현재 `0`으로 설정하며, 작성자 외 승인자가 반드시 있어야 하는 정책은 강제하지 않는다. PR과 검사 의무는 그대로 적용되고, `main` 승격의 판단과 실행은 병합 담당자가 맡는다. 팀의 리뷰 담당자가 정해지면 필수 승인자 수를 조정할 수 있다.

`branch-policy`는 `main` PR의 출발점이 같은 저장소의 `develop`인지 검사한다. `develop`에는 기능 브랜치와 같은 저장소 `main`의 동기화 PR을 허용한다. 검사에서는 PR 코드나 외부 스크립트를 실행하지 않고 GitHub 이벤트의 브랜치 정보만 읽는다. 실제 기능 검증은 기존 `test` 작업이 수행한다.

이 설정의 근거와 지원 기능은 [GitHub 브랜치 보호 안내](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)에서 확인한다.
