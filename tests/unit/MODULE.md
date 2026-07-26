# 단위 테스트 모듈

## 목적과 책임

외부 I/O 없이 하나의 모듈 인터페이스와 순수 로직을 빠르고 결정 가능하게 검증한다.

## 포함할 구현

Task/WorkflowRun/AgentRun 생명주기, runtime 환경 설정, HTTP schema의 순수 validation·변환·registry와 import 방향 테스트를 포함한다. Compiled graph를 실행하는 supervisor 테스트는 integration 영역에 둔다.

## 공개 인터페이스와 사용 방법

제품 모듈 구조와 대응되는 테스트 파일명을 사용한다. `tests.unit` package marker를 유지해 루트의 표준 `unittest discover`가 단위 테스트를 수집하게 한다.

## 의존성과 허용된 import 방향

대상 공개 인터페이스와 작은 fake만 사용한다. 실제 네트워크와 영구 데이터베이스에 의존하지 않는다.

## 데이터 및 제어 흐름

준비된 입력을 대상 인터페이스에 전달하고 반환 결과와 기록된 호출을 검증한다.

## 설계 결정과 제약사항

테스트 사이에 상태를 공유하지 않고 시간·난수·모델 출력을 통제한다.

## 테스트 전략

정상, 오류, 경계값과 불변 조건을 독립된 사례로 검증한다. 생명주기 테스트는 모든 status·phase 조합과 Approval 및 budget 경계를 표 기반으로 다룬다. planless Task의 상태별 정확한 version과 planful 반복 갱신을 함께 검증하며, AgentRun은 직접 생성 차단, 여섯 issuance binding 필드의 변이 거부, historical/current 복원과 malformed WorkflowRun issuance snapshot을 검증한다. AST 기반 architecture test는 concrete provider·저장·전송 adapter import를 차단하고 HTTP가 runtime facade 외 내부 구현을 import하지 않는지 검증한다.

## 변경 시 문서 갱신 조건

단위 테스트 범위나 fake 사용 원칙이 바뀔 때 갱신한다.
