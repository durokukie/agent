# Models 모듈

## 목적과 책임

설정에 따라 LangChain 호환 chat model을 생성하고 공통 실행 옵션을 적용한다.

## 포함할 구현

provider별 생성 설정, timeout, retry와 모델 capability 검증을 포함한다.

## 공개 인터페이스와 사용 방법

runtime이 설정을 전달해 model 인스턴스를 생성하고 agent와 supervisor에 주입한다.

## 의존성과 허용된 import 방향

`config`와 외부 모델 SDK에 의존할 수 있다. agent나 orchestration을 import하지 않는다.

## 데이터 및 제어 흐름

설정값을 provider adapter의 생성 인자로 변환하고 준비된 model을 반환한다.

## 설계 결정과 제약사항

provider별 차이를 호출자에게 퍼뜨리지 않되 실제 provider가 하나일 때 불필요한 자체 인터페이스는 만들지 않는다.

## 테스트 전략

설정 변환과 필수 capability 검증을 fake model 또는 SDK mock으로 확인한다.

## 변경 시 문서 갱신 조건

지원 provider, 생성 옵션, retry 또는 capability 정책이 바뀔 때 갱신한다.
