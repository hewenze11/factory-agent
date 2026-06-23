# factory-agent

AI Software Factory - User Server Agent

用户服务器上运行的轻量 Python Agent，提供：
- Git-based 文件管理系统（init / write / commit / log / export）
- 安全路径校验（7步防穿越）
- Token 鉴权
- Trash GC（7天/10GB自动清理）
- 运维监控数据推送

## 镜像

```
172.236.254.239:30880/factory/agent:latest
```

## 构建

推送到 `main` 分支自动触发 CI，镜像推到 Harbor。
