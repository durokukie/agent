"""등록된 클러스터로 실행하기 (기획 04 §8, issue #61).

클러스터 등록·kubeconfig 검사·자격증명 암호화는 Kukie API 서버(kukie-server)가 한다 (DURO-113).
agent 에 남은 것은 실행 쪽뿐이다.
  access.py   실행 직전에 server 에서 연결 정보를 받아 임시 kubeconfig 를 연다
  runtime.py  임시 kubeconfig 파일을 만들고 끝나면 지운다. 강제 종료로 남은 파일은 다음 기동 때 지운다
  overview.py 개요(GET /clusters/{id}/overview)의 kubectl JSON 을 대시보드 응답으로 정리한다 (순수 함수, CNI 알아보기 포함)
"""
