# 内外网同步与运行

项目把环境差异放在未入库的 `.env` 和安装环境变量中。外网开发、push 后，
内网执行 `git pull` 不会覆盖已有 `.env`，因此无需反复修改受版本控制的配置文件。

## 首次准备

```bash
cp .env.example .env
```

编辑 `.env`，填写内网 OpenAI-compatible 网关地址、API key 和模型名。当前节点均绑定
`ark_primary`，因此至少需要配置 `SQLMATE_PRIMARY_*`。自签名证书场景才将
`SQLMATE_PRIMARY_VERIFY_SSL` 设为 `false`；公网服务必须保持 `true`。

若机器已有完整依赖环境，可直接指定解释器：

```bash
SQLMATE_PYTHON=/path/to/venv/bin/python ./sqlmate --help
```

当前开发机也会自动兼容 `/home/remhero/bin/.venv/bin/python`。正式内网机器建议在项目
内创建 `.venv`，这样 `./sqlmate` 会优先使用它：

```bash
./install.sh
./sqlmate --help
```

## 内网离线/镜像安装

内部 PyPI 镜像：

```bash
SQLMATE_PIP_INDEX_URL=https://pypi-mirror.example.com/simple/ \
SQLMATE_PIP_TRUSTED_HOST=pypi-mirror.example.com \
./install.sh
```

系统 Python 缺少 SSL 或版本低于 3.10 时，可使用内网已有 Python 源码和 OpenSSL：

```bash
SQLMATE_PYTHON_SOURCE_DIR=/path/to/Python-3.10.x \
SQLMATE_OPENSSL_ROOT=/path/to/openssl-root \
SQLMATE_OPENSSL_LIB_DIR=/path/to/openssl/lib \
./install.sh
```

运行时若 `_ssl` 依赖非系统 OpenSSL，同样设置 `SQLMATE_OPENSSL_LIB_DIR`；可以写入
服务器用户的 shell 环境，或在启动前导出。

## 日常同步流程

```bash
git pull --ff-only
./install.sh       # requirements.txt 有变化时执行
./sqlmate
```

`.venv/`、`.python310/`、`logs/`、`output/`、vendored OpenSSL 和临时 patch 都已忽略，
不会再把机器相关二进制或运行日志推回代码仓。
