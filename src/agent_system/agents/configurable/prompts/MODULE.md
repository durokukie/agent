# 설정형 Agent Prompt 모듈

## 목적과 책임

설정형 agent가 사용하는 versioned prompt 자료를 관리한다.

## 포함할 구현

시스템 prompt, 역할별 prompt 조각과 필요한 metadata를 포함한다.

## 공개 인터페이스와 사용 방법

설정형 agent의 prompt loader만 이 디렉터리의 자료를 읽는다.

## 의존성과 허용된 import 방향

Python 모듈에 의존하지 않는다. prompt에서 특정 adapter나 비밀정보를 가정하지 않는다.

## 데이터 및 제어 흐름

선택된 prompt가 설정과 결합되어 모델 요청에 전달된다.

## 설계 결정과 제약사항

prompt 변경은 동작 변경으로 취급하며 목적과 예상 출력을 검증 가능하게 유지한다.

## 테스트 전략

필수 변수, 렌더링 결과, 금지된 미정의 placeholder와 대표 행동 평가를 검증한다.

## 변경 시 문서 갱신 조건

prompt 체계, 변수, 로딩 또는 versioning 방식이 바뀔 때 갱신한다.
