#!/usr/bin/env bash
# 构建 → 推送 → 部署到 k0s 集群（节点 k0s-share1，containerd 从 Gitea registry 拉镜像）。
#
#   ./k8s/deploy.sh            # 用 pyproject.toml 里的 version 作镜像 tag
#   ./k8s/deploy.sh 1.0.1      # 指定 tag
#
# 这是手动/本地那条路；推 main 后 CI（.gitea/workflows/build-image.yaml）也会构建并推
# 同样的版本 tag 到同一个 registry。
# 只用 kubectl + docker，Secret 现场从 .env 生成，永远不会落到仓库里。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

REGISTRY="coding.fly97.fun:8443"   # Gitea 自带的容器 registry（nanorouter/scraper 同款）
IMAGE="$REGISTRY/fly97/glm-usage"
NAMESPACE="default"
ENV_FILE="$ROOT/.env"
MANIFEST="$ROOT/k8s/deployment.yaml"

die() { echo "✗ $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

command -v kubectl >/dev/null || die "没有 kubectl"
command -v docker >/dev/null || die "没有 docker"

TAG="${1:-$(python3 -c "import tomllib,pathlib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version'])")}"

[ -f "$ENV_FILE" ] || die "缺少 .env（里面要有 BIGMODEL_TOKEN）：cp .env.example .env 后填好"
grep -qE '^BIGMODEL_TOKEN=.+' "$ENV_FILE" || die ".env 里的 BIGMODEL_TOKEN 是空的"

# 1) Secret：只挑需要的那两个键，走临时文件而不是命令行参数，
#    免得 token 出现在 ps / shell history 里。
step "生成 glm-usage-secret（值来自 .env）"
tmp_env="$(mktemp)"
trap 'rm -f "$tmp_env"' EXIT
chmod 600 "$tmp_env"
grep -E '^(BIGMODEL_TOKEN|GLM_USAGE_API_KEY)=.+' "$ENV_FILE" > "$tmp_env"
kubectl -n "$NAMESPACE" create secret generic glm-usage-secret \
  --from-env-file="$tmp_env" \
  --dry-run=client -o yaml | kubectl apply -f -

# 2) 镜像。--network=host 是这台机器的老毛病：dockerd 给构建容器下发的是失效的
#    Tailscale DNS（100.100.100.100），解析不了 pypi.org，走宿主网络才能 pip install。
step "构建并推送 $IMAGE:$TAG"
docker build --network=host -t "$IMAGE:$TAG" "$ROOT"
docker push "$IMAGE:$TAG" || die "push 失败：先 docker login $REGISTRY"

# 3) 清单：把镜像 tag 对齐成刚推上去的那个，再交给 kubectl。
#    IngressRoute 是 Traefik 的 CRD，--dry-run=server 会真的问一遍 API Server
#    校验字段，避免 apply 到一半才发现写错。
step "部署清单（namespace=$NAMESPACE, image=$IMAGE:$TAG）"
sed "s|^\( *image: \)${IMAGE}:.*$|\1${IMAGE}:${TAG}|" "$MANIFEST" \
  | kubectl -n "$NAMESPACE" apply --server-side --dry-run=server -f - >/dev/null
sed "s|^\( *image: \)${IMAGE}:.*$|\1${IMAGE}:${TAG}|" "$MANIFEST" \
  | kubectl -n "$NAMESPACE" apply --server-side -f -

step "等待滚动完成"
kubectl -n "$NAMESPACE" rollout status deployment/glm-usage --timeout=180s

cat <<EOF

✔ 完成。看板: https://glm-usage.fly97.fun/dashboard
  带一把 key 打开（面板会把 key 存进 localStorage，之后就不用再带了）:
    https://glm-usage.fly97.fun/dashboard?key=\$(grep '^GLM_USAGE_API_KEY=' .env | cut -d= -f2)
  日志: kubectl -n $NAMESPACE logs -f deploy/glm-usage
  换 token 后重跑本脚本即可（只生成 Secret，token 变了 reloader 会自动重启 Pod）。
EOF
