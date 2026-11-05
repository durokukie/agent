# 이 레포에서 AI 가 지킬 것

## 저장은 kukie-server 내부 API 로

- agent 는 DB 를 갖지 않는다. 대화·run·Action Plan·클러스터의 읽기·쓰기와 권한 판단은 전부 `kukie_agent/server_api.py` 를 거쳐 kukie-server `/internal/**` 로 한다 (DURO-113)
- 저장할 것이 바뀌면 kukie-server 의 엔드포인트·Flyway 마이그레이션이 먼저다. agent 는 `server_api.py` 에 함수를 더하고, `tests/fake_server.py` 에 같은 이름의 가짜를 더한다 (`server_api.API` 목록)
- plan·run 의 다음 상태는 server 가 정한다. agent 는 일어난 일을 동작(`dry-run`·`offer`·`decision`·`start`·`result` …)으로 알리고, 돌아온 값으로 메모리를 맞춘다
- 부를 때는 `server_api.x(...)` 로 쓴다 — 테스트가 모듈 속성을 가짜로 바꿔 끼우므로 `from kukie_agent.server_api import x` 로 가져온 이름은 진짜 server 를 부른다
- 사용자 Bearer 는 입구 호출(`user=` 인자)에만 싣는다 — access token 은 30분이면 만료되고, run 도중의 기록은 내부 토큰만으로 끝까지 가야 한다
