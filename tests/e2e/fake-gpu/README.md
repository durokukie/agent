# 가짜 GPU kind 클러스터 (DURO-118)

GPU 없이 개요의 `gpu` 칸과 앱의 GPU 화면을 확인하는 절차. 노드 status 에 `nvidia.com/gpu` 를 붙이고,
DCGM exporter·vLLM 대신 테스트 픽스처(`tests/fixtures/overview/dcgm.txt`·`vllm.txt`)를 `/metrics` 로 내주는 busybox 파드를 띄운다.

```bash
# 레포 맨 위에서
kind create cluster --name kukie-gpu-fake
C=kind-kukie-gpu-fake; N=kukie-gpu-fake-control-plane
kubectl --context $C patch node $N --subresource=status --type=json -p \
  '[{"op":"add","path":"/status/capacity/nvidia.com~1gpu","value":"1"},{"op":"add","path":"/status/allocatable/nvidia.com~1gpu","value":"1"}]'
kubectl --context $C label node $N nvidia.com/gpu.product=NVIDIA-L40S nvidia.com/gpu.memory=46068 nvidia.com/gpu.count=1

# 멀티 아키텍처 이미지를 그대로 kind load 하면 실패해서 한 번 빌드해 단일 아키텍처로 만든다
printf 'FROM busybox:1.37\n' | docker build -q -t local/fake-dcgm-exporter:test -
printf 'FROM busybox:1.37\n' | docker build -q -t local/fake-vllm-openai:test -
kind load docker-image --name kukie-gpu-fake local/fake-dcgm-exporter:test local/fake-vllm-openai:test

kubectl --context $C create namespace gpu-operator
kubectl --context $C -n gpu-operator create configmap fake-dcgm-metrics --from-file=metrics=tests/fixtures/overview/dcgm.txt
kubectl --context $C -n default create configmap fake-vllm-metrics --from-file=metrics=tests/fixtures/overview/vllm.txt
kubectl --context $C apply -f tests/e2e/fake-gpu/fake-gpu.yaml
```

kubelet 이 다시 뜨면 노드 status 의 `nvidia.com/gpu` 가 사라질 수 있다 — 그때는 patch 를 다시 한다.
