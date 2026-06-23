FROM python:3.11-slim

# 创建 factory 用户
RUN useradd -m factory

# 安装 git
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖文件，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源代码
COPY . .

# 创建运行时目录并授权
RUN mkdir -p /opt/factory/.factory /opt/factory/repos /opt/factory/trash \
    && chown -R factory:factory /opt/factory \
    && chown -R factory:factory /app

USER factory

# 容器内监听 0.0.0.0 以支持 Docker 端口映射
# 宿主机直接运行时可覆盖为 127.0.0.1（不对外暴露）
ENV FACTORY_BIND_HOST=0.0.0.0

EXPOSE 34567

CMD ["python", "agent.py"]
