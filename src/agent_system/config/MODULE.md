# Config 모듈

## 목적과 책임

환경변수와 설정 파일을 타입이 있는 애플리케이션 설정으로 변환하고 시작 시 검증한다. 현재는 runtime composition에 필요한 SQLite 경로, 실행 용량과 notification lease timing을 `RuntimeSettings`로 제공한다.

## 포함할 구현

`RuntimeSettings`, `RuntimeConfigurationError`와 환경변수 파싱을 포함한다. `RuntimeSettings`는 SQLite DB 파일 경로, model 설정, 최대 agent 실행 수, 대기열 용량, worker 수와 notification lease·timeout·heartbeat 초를 보관한다.

## 공개 인터페이스와 사용 방법

runtime은 시작 시 `RuntimeSettings.from_env()`를 한 번 호출하고 필요한 하위 설정을 각 모듈에 주입한다. 선택적으로 `Mapping[str, str]`을 넘겨 process 환경 대신 결정 가능한 입력을 사용할 수 있다.

`AGENT_SYSTEM_DB_PATH`는 필수이며, 존재하는 디렉터리가 아니고 부모 디렉터리가 존재하는 SQLite 파일 경로여야 한다. 대상 파일 자체는 아직 없어도 된다. `AGENT_MAX_RUNS`, `AGENT_QUEUE_CAPACITY`, `AGENT_WORKER_COUNT`는 각각 기본값 `3`, `100`, `1`의 양의 10진 정수이고 상한은 각각 `1000`, `10000`, `128`이다. 공백, 부호, 소수, boolean을 포함한 정수가 아닌 값은 거부한다. 같은 mapping은 `ModelSettings.from_env()`에도 전달되어 `model_settings`를 구성한다.

`AGENT_NOTIFICATION_LEASE_SECONDS`, `AGENT_NOTIFICATION_SEND_TIMEOUT_SECONDS`,
`AGENT_NOTIFICATION_HEARTBEAT_SECONDS` 기본값은 `30`, `20`, `5`다. 모두 `3600` 이하의
양의 정수이며 timeout은 lease보다 작고 heartbeat 두 주기는 lease보다 작아야 한다.

## 의존성과 허용된 import 방향

표준 라이브러리와 provider 중립 `agent_system.models.ModelSettings`에만 의존한다. 업무 모듈이나 adapter를 import하지 않는다.

## 데이터 및 제어 흐름

환경 입력을 파싱·검증하고 `ModelSettings.from_env()`와 조합해 frozen `RuntimeSettings` 객체를 만들고 composition root로 전달한다.

## 설계 결정과 제약사항

비밀값을 기본값이나 로그에 포함하지 않는다. `ModelSettings`의 API key는 `RuntimeSettings` repr에도 노출되지 않으며 runtime 설정 오류 메시지에도 입력값을 포함하지 않는다. 누락되거나 잘못된 필수 설정은 실행 시작 전에 `RuntimeConfigurationError`로 실패시킨다.

## 테스트 전략

기본값, mapping 주입, model 설정 조합, 불변성, DB 경로와 부모 검증, 정수 형식·양수·상한,
notification timing 관계, 비밀정보 비노출을 단위 테스트한다.

## 변경 시 문서 갱신 조건

설정 항목, 기본값·상한, source 우선순위, validation 또는 비밀정보 정책이 바뀔 때 갱신한다.
