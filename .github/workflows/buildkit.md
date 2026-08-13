# buildctl 在 GitHub Actions Runner 中的使用指南

## 概述

在 vllm-ascend 项目的 GitHub Actions workflow 中，我们已配置 buildctl 客户端来与 buildkitd 服务进行通信，用于构建和推送容器镜像。

## 前置条件

- 使用 `linux-amd64-cpu-16-hk` 或 `linux-amd64-cpu-32-hk` 标签的 GitHub Actions Runner
- buildkitd 服务已在 buildkitd namespace 部署，地址为 `tcp://buildkitd-service.buildkitd:1234`
- Vault 已配置了 buildkitd 证书和认证信息

## buildctl 自动安装

Pod Template 中的 initContainer 会自动安装 buildctl：
- 版本: v0.29.0
- 安装路径: `/usr/local/bin/buildctl`
- 自动检测 CPU 架构（amd64/arm64）

## Workflow 示例

### 基础构建和推送示例

```yaml
name: Build and Push Image with buildctl

on:
  push:
    branches: [ main ]

jobs:
  build:
    runs-on: [self-hosted, linux-amd64-cpu-16-hk]
    steps:
      - uses: actions/checkout@v4

      - name: Build and Push Image
        run: |
          set -ex
          
          # 设置变量
          IMAGE_REGISTRY="swr.cn-southwest-2.myhuaweicloud.com/modelfoundry"
          IMAGE_NAME="vllm-ascend"
          IMAGE_TAG="${GITHUB_SHA:0:7}"
          
          # buildctl 已自动安装，可直接使用
          buildctl \
            --addr="${BUILDKITD_ADDR}" \
            --tlscacert="${DOCKER_CONFIG}/ca.pem" \
            --tlscert="${DOCKER_CONFIG}/cert.pem" \
            --tlskey="${DOCKER_CONFIG}/key.pem" \
            build \
            --frontend dockerfile.v0 \
            --local context=. \
            --local dockerfile=./docker \
            --opt filename=Dockerfile \
            --secret id=dockerconfig,src="${DOCKER_CONFIG}/config.json" \
            --output type=image,name="${IMAGE_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}",push=true \
            --progress=plain
          
          echo "Image pushed: ${IMAGE_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"
```

### 高级示例：带构建参数和缓存

```yaml
      - name: Build with Cache
        run: |
          set -ex
          
          buildctl \
            --addr="${BUILDKITD_ADDR}" \
            --tlscacert="${DOCKER_CONFIG}/ca.pem" \
            --tlscert="${DOCKER_CONFIG}/cert.pem" \
            --tlskey="${DOCKER_CONFIG}/key.pem" \
            build \
            --frontend dockerfile.v0 \
            --local context=. \
            --local dockerfile=./docker \
            --opt filename=Dockerfile.ci \
            --opt build-arg:BUILDKIT_INLINE_CACHE=1 \
            --opt build-arg:PYTHON_VERSION=3.10 \
            --secret id=dockerconfig,src="${DOCKER_CONFIG}/config.json" \
            --secret id=github_token,env=GITHUB_TOKEN \
            --output type=image,name=${IMAGE_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG},push=true,image-manifest=true \
            --progress=plain
```

## 环境变量

以下环境变量自动注入到 job 容器中：

- `BUILDKITD_ADDR`: buildkitd 服务地址（tcp://buildkitd-service.buildkitd:1234）
- `DOCKER_CONFIG`: Docker 配置目录（/home/user/.docker）

## 证书和认证

Vault 注入的文件位置：`${DOCKER_CONFIG}` （即 `/home/user/.docker`）

- `ca.pem`: buildkitd Root CA 证书
- `cert.pem`: 客户端证书
- `key.pem`: 客户端私钥
- `config.json`: Docker 认证配置

## buildctl 常用命令

```bash
# 查看版本
buildctl --version

# 列出缓存
buildctl du --verbose

# 清理缓存
buildctl prune

# 获取帮助
buildctl build --help
```

## 常见问题

### 1. buildctl: command not found

Pod Template 中的 initContainer 会自动安装 buildctl。如果仍然找不到，请检查：
- Runner 是否使用了正确的标签（linux-amd64-cpu-16-hk 或 linux-amd64-cpu-32-hk）
- initContainer 是否正确运行

### 2. TLS 证书错误

确保：
- Vault 中的 buildkitd secret 配置正确
- ServiceAccount 有正确的角色和权限
- `vault.hashicorp.com/role: ascend-gha-runners` 注解存在

### 3. 连接 buildkitd 失败

检查：
- buildkitd 服务是否运行：`buildkitd-service.buildkitd:1234`
- Kubernetes 网络策略是否允许连接
- buildkitd pod 的日志

### 4. 权限错误

如果遇到权限问题：
- 确保 `/home/user/.docker` 目录权限正确（Vault 注解会自动设置为 0400）
- Runner service account 有足够的权限访问所需资源

## 最佳实践

1. **使用 secret 传递敏感信息**
   ```bash
   buildctl build ... --secret id=token,env=MY_TOKEN ...
   ```

2. **启用缓存优化**
   ```bash
   --opt build-arg:BUILDKIT_INLINE_CACHE=1
   ```

3. **使用具体的 buildctl 版本**
   在 Pod Template 中指定 BUILDKIT_VERSION，而不是使用 latest

4. **监控构建进度**
   使用 `--progress=plain` 获取详细的构建过程输出

## 参考资源

- [buildkit GitHub](https://github.com/moby/buildkit)
- [buildctl 文档](https://github.com/moby/buildkit/tree/master/cmd/buildctl)
- [Dockerfile 前端](https://github.com/moby/buildkit/tree/master/frontend/dockerfile)
