# 测试环境部署说明

该目录保存测试服务器部署参考。敏感连接信息通过 `.env` 或服务器环境变量提供，不提交到 Git。

## 服务进程

- API: `python scripts/run_mock_api.py`
- 默认端口：`8091`
- 健康检查：`GET /health`

## 测试

```bash
pytest
python scripts/smoke_test_api.py --base-url http://127.0.0.1:8091
```

