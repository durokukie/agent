"""클러스터 등록과 kubeconfig 보관 (기획 04 §8, issue #61).

지금까지 agent 는 자기가 돌고 있는 컴퓨터의 ~/.kube/config 를 읽었다. 서버가 사용자 노트북 밖으로
나가면 그 파일이 없으므로, 사용자가 클러스터를 등록하고 서버가 접속 정보를 보관해야 한다.

여기 모인 것:
  kubeconfig.py  업로드한 kubeconfig 를 검사하고 context 하나로 정규화한다
  crypto.py      자격증명을 서버 키로 대칭 암호화한다 (키 없으면 기동 거부)
  runtime.py     실행 직전에 임시 kubeconfig 파일을 만들고 끝나면 지운다
"""
