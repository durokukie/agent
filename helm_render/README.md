# 렌더링 서비스 (helm_render)

고객 Helm 차트를 렌더링만 하는 작은 API다 (DURO-137). 에이전트와 다른 파드에서 돈다.

- 에이전트는 모든 고객이 함께 쓰는 한 대이고, 대화 잠금과 승인 대기가 프로세스 메모리에 있다. 나쁜 차트로 재시작되면 전체 대화가 끊긴다. 그래서 나쁜 차트는 이 파드만 실패하게 한다.
- 고객 차트가 접속 정보와 같은 공간에 있으면 안 된다. 이 파드에는 비밀값과 서비스 계정 토큰이 없다.
- 저장하는 것이 없다. 요청마다 임시 폴더를 만들고 끝나면 지운다.
- 바깥으로 나가는 길이 없다. NetworkPolicy로 나가는 길을 전부 막고, 들어오는 길은 에이전트만 둔다. 차트는 에이전트가 받아 넘긴다.

에이전트 쪽 호출은 `kukie_agent/manifests/helm.py`, 배포는 `kukie-manifest`의 `base/helm-render`다.

## API

| 경로 | 입력 | 출력 |
|---|---|---|
| `POST /render` | 차트(tgz, base64), values 파일 목록, 렌더링 조건 | 성공 200 YAML, 실패 `{reason, message}` |
| `GET /health` | | 상태와 helm 버전 |

렌더링 조건은 Argo CD Application에서 온다: `release_name`, `namespace`, `kube_version`, `api_versions`, `skip_crds`, `skip_schema_validation`, `skip_tests`. `value_files`는 받은 순서가 helm 병합 순서다. 인자는 Argo CD `util/helm/cmd.go`의 `template`과 같다.

성공 응답의 `X-Helm-Version`이 어느 helm 기준의 결과인지 알려 준다.

## 제한

값은 전부 시작값이고 환경변수 `HELM_RENDER_<이름>`으로 바꾼다 (`kukie_helm_render/config.py`).

| 항목 | 시작값 | 넘으면 |
|---|---|---|
| 동시 렌더링 | 2개 | 기다리지 않고 429 `busy` |
| 시간 | 5초 | 프로세스 종료, 504 `timeout` |
| 출력 | 20MB | 읽는 도중 종료, 422 `output_too_large` |
| helm 한 프로세스 메모리 | 220MiB | 종료, 422 `memory_exceeded` |
| 파드 메모리 | 512Mi (kukie-manifest) | 파드 재시작. 호출한 쪽에는 연결 실패로 보인다 |
| 차트 압축 파일 / 푼 합계 / 파일 수 | 16MiB / 64MiB / 5000개 | 413 또는 422 |

## 막는 것

- **쓰지 않는 옵션:** `--enable-dns`, `--dry-run=server`, `--post-renderer`, `--dependency-update`, 플러그인. 입력 값은 형식 검사를 통과해야 helm 인자가 된다.
- **심볼릭 링크와 차트 밖 경로:** 어떤 링크든, 안쪽 `charts/*.tgz`까지 거절한다. helm 폴더 로더는 링크를 따라가 차트 밖 파일을 템플릿으로 읽는다.
- **스키마의 `$ref`, `$schema`:** 파일과 http(s) 주소를 읽는다(helm 4.2로 확인). 문서 안(`#…`)과 helm이 아는 메타 스키마만 받는다. 다음 겹으로 helm의 프록시를 닿지 않는 주소로 두고, 그다음 겹이 NetworkPolicy다. 자기 파드의 loopback은 NetworkPolicy로 막을 수 없어 `$ref` 검사가 맡는다. 이 파드의 loopback에는 이 서비스뿐이다.
- **values 경로 조작(CVE-2022-24348):** 값을 파일 내용으로 받고 파일 이름은 이 서비스가 짓는다.
- **네트워크:** 나가는 길을 NetworkPolicy로 막는다(`kukie-manifest`의 `base/helm-render`). 차트 내려받기는 에이전트의 `kukie_agent/manifests/chart_fetch.py`가 한다.
- **로그:** 렌더링 결과, values, helm stderr를 남기지 않는다. 사유와 시간, 크기만 남긴다.

`Chart.yaml`의 의존성이 `charts/`에 없으면 `missing_dependency`로 실패한다. Argo CD는 이때 `helm dependency build`를 돌리지만 여기서는 돌리지 않는다.

## 시험

helm이 PATH에 있어야 한다. CI는 Dockerfile과 같은 버전을 깐다.

```bash
cd helm_render && uv run pytest
```

파드와 같은 조건으로 띄우는 법:

```bash
docker build -t kukie-helm-render:local helm_render
```

```bash
docker run --rm -p 18080:8000 --memory=512m --read-only --user 1000 --cap-drop=ALL --tmpfs /work:uid=1000,gid=1000,mode=700 kukie-helm-render:local
```

helm을 올릴 때는 Dockerfile의 버전과 sha256, `.github/workflows/helm-render.yml`을 함께 바꾼다.
