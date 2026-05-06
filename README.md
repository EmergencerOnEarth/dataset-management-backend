# DE_sys 开发工作区

本目录后续作为数据集管理后端开发工作区使用。当前已按文档、参考资料、测试数据分区整理。

## 目录结构

| 路径 | 内容 |
| --- | --- |
| `docs/requirements/` | 需求文档原件，包括 V1.2.3、V1.2.7 版本 |
| `docs/interface-list/` | 前端接口清单初稿，例如 `V2.1数据集导入模块需支持接口.xlsx` |
| `docs/design/` | 已产出的接口文档、设计文档及对应 Markdown/Word 版本 |
| `docs/references/newvision/` | 新视野数据对接参考资料，包括 json 字段说明和 dat 解析参考代码 |
| `test-data/upload-samples/local/` | 当前需求对应的测试上传数据和 zip 包 |
| `test-data/upload-samples/newvision/` | 新视野提供的院外导入、OCT、生物测量、眼底照相等样例数据 |

## 使用建议

- 后端接口、数据库脚本、服务代码后续可以直接在根目录下新增标准工程目录，例如 `src/`、`db/`、`scripts/`、`tests/`。
- 大体量上传样例统一从 `test-data/upload-samples/` 读取，避免和需求/设计文档混放。
- 需求和设计文档统一从 `docs/` 查找，后续版本继续按子目录归档。

## 数据集管理后端 Mock 服务

当前后端工程实现了数据集管理 V0.2.0 设计文档中的 17 个接口，服务真实启动并处理 HTTP 请求，业务数据暂用内存 mock 数据返回；上传分片接口会真实写入 `.runtime/` 目录。

### 本地开发

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'fastapi>=0.110' 'uvicorn>=0.27' 'pydantic-settings>=2.2' 'python-multipart>=0.0.9' 'pytest>=8.0' 'httpx>=0.27'
.venv/bin/python -m pytest
env APP_HOST=127.0.0.1 APP_PORT=8091 .venv/bin/python scripts/run_mock_api.py
```

### 接口冒烟测试

```bash
.venv/bin/python scripts/smoke_test_api.py --base-url http://127.0.0.1:8091
```

### 配置说明

- `.env.example` 是可提交的配置模板。
- `.secrets/local.env` 保存本地 GitHub、测试服务器、数据库、FTP 等敏感信息，已被 `.gitignore` 排除。
- `test-data/` 和 `docs/references/` 体量较大，默认不提交到 Git。
