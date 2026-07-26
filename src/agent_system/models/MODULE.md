# Models 모듈

## 목적과 책임

설정에 따라 LangChain 호환 chat model을 생성하고 공통 실행 옵션을 적용한다.

## 포함할 구현

`ModelSettings`, `ModelCapability`, `create_chat_model()`과 결정 가능한 `FakeChatModel`을 포함한다. 현재 provider adapter는 Upstage만 지원한다.

## 공개 인터페이스와 사용 방법

runtime은 `ModelSettings.from_env()`로 `MODEL_PROVIDER`, `MODEL_NAME`, `UPSTAGE_API_KEY`, `MODEL_TIMEOUT_SECONDS`, `MODEL_MAX_RETRIES`를 읽고 `create_chat_model()`에 전달한다. factory는 필수 capability를 함께 받아 검증하고 `BaseChatModel`을 반환한다. 외부 I/O가 필요 없는 호출자 테스트에는 고정 응답과 받은 message를 제공하는 `FakeChatModel`을 사용한다.

timeout은 `bool`이 아닌 finite 양수이고 retry 횟수는 `bool`을 제외한 0 이상의 정수여야 한다. 잘못된 값은 SDK 생성 전에 항상 `ModelConfigurationError`로 정규화한다. 지원하지 않는 capability는 `UnsupportedModelCapabilityError`로 거부한다.

## 의존성과 허용된 import 방향

LangChain Core의 `BaseChatModel`에 공개 seam으로 의존하고 factory 내부에서만 `langchain_upstage.ChatUpstage`를 import한다. agent, classifier와 orchestration은 Upstage를 import하지 않고 주입된 `BaseChatModel`만 사용한다. 이 모듈은 agent나 orchestration을 import하지 않는다.

## 데이터 및 제어 흐름

환경 변수는 검증된 `ModelSettings`로 변환된다. factory는 provider와 필수 capability를 확인한 뒤 timeout과 retry를 포함한 설정을 Upstage adapter 생성 인자로 변환하고 준비된 model을 반환한다. API key는 repr에서 제외하며 오류 메시지에 값을 포함하지 않는다.

## 설계 결정과 제약사항

provider별 차이를 호출자에게 퍼뜨리지 않으며 자체 model interface 대신 LangChain `BaseChatModel`을 seam으로 사용한다. `MODEL_PROVIDER=upstage`만 지원한다. Upstage adapter는 tool calling과 structured output capability를 제공하고 image input 요구는 현재 거부한다. timeout과 retry 실행 자체는 SDK에 위임하되 값의 형식과 범위는 이 모듈 경계에서 검증한다.

## 테스트 전략

환경 설정 변환, 필수값, timeout, retry와 capability 정책을 단위 테스트한다. factory가 timeout과 retry를 Upstage 생성자에 전달하는지 adapter 경계의 spy로 검증한다. `FakeChatModel`의 고정 응답과 요청 기록을 검증한다. 모든 실제 Upstage adapter 생성 테스트는 DNS와 socket 연결을 process-local guard로 차단하고 invoke하지 않아 기본 suite에서 외부 API를 호출하지 않는다.

## 변경 시 문서 갱신 조건

지원 provider, 생성 옵션, retry 또는 capability 정책이 바뀔 때 갱신한다.
