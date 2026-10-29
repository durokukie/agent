# 매니페스트 레포 픽스처 (DURO-133)

`tests/test_manifests.py` 가 이 폴더를 매니페스트 레포처럼 쓴다. 모양은 durokukie/kukie-gitops-sandbox 와 kukie-manifest 를 따랐다.

- `apps/plain` — ArgoCD Directory 형식. web(HPA 없음) · worker(HPA 있음) · Service
- `apps/kustomize/base` — 두 환경이 같이 쓰는 base. api(nginx) · agent(kukie-manifest 처럼 이름만 있는 이미지)
- `apps/kustomize/overlays/prod` — Application path. replicas · images(newTag, newName + digest) · patch
- `apps/kustomize/overlays/dev` — 다른 환경. base 를 고치면 여기도 바뀌어 렌더 비교에 걸린다
