# DE_sys 开发工作区

本目录后续作为数据集管理后端开发工作区使用。当前已按文档、参考资料、测试数据分区整理。

## 目录结构

| 路径 | 内容 |
| --- | --- |
| `docs/requirements/` | 需求文档原件，包括 V1.2.3、V1.2.7 版本 |
| `docs/interface-list/` | 前端接口清单初稿，例如 `V2.1数据集导入模块需支持接口.xlsx` |
| `docs/design/` | 已产出的接口文档、设计文档及对应 Markdown/Word 版本 |
| `docs/development/` | 本地开发环境记录（如 MySQL 连接说明） |
| `docs/references/newvision/` | 新视野数据对接参考资料，包括 json 字段说明和 dat 解析参考代码 |
| `test-data/upload-samples/local/` | 当前需求对应的测试上传数据和 zip 包 |
| `test-data/upload-samples/newvision/` | 新视野提供的院外导入、OCT、生物测量、眼底照相等样例数据 |

## 使用建议

- 后端接口、数据库脚本、服务代码后续可以直接在根目录下新增标准工程目录，例如 `src/`、`db/`、`scripts/`、`tests/`。
- 大体量上传样例统一从 `test-data/upload-samples/` 读取，避免和需求/设计文档混放。
- 需求和设计文档统一从 `docs/` 查找，后续版本继续按子目录归档。

## 本地开发数据库（MySQL）

开发与本地联调使用 **MySQL**（与本机安装的实例一致）；**测试环境**与**生产环境**使用 TiDB 或团队指定的 MySQL 兼容数据库。TiDB 在 DDL、多数 DML 上与 MySQL 8 方言接近，可在本机先用 MySQL 跑通表结构与业务 SQL，再上测试环境做差异验证。

数据集管理相关表与连接 **使用独立库 `eye_research_dataset`**，不要写入本机已有的 `medical_data` 等业务库，避免表名与数据冲突。

### 本机当前连接参数（示例）

来源：开发机本地实例（路径与版本随 Homebrew 安装可能变化）。

| 项 | 值 |
| --- | --- |
| 主机 | `127.0.0.1`（localhost） |
| 端口 | `3306` |
| 用户名 | `root` |
| 密码 | 无（空密码） |
| **数据库名（本模块专用）** | **`eye_research_dataset`** |
| 字符集 | `utf8mb4` |
| Socket（本机） | `/tmp/mysql.sock` |
| MySQL 版本（示例） | `9.x`（Homebrew） |
| 服务启停（示例） | `brew services start mysql` / `brew services stop mysql` |

首启前在本机创建专用库（一次性）：

```sql
CREATE DATABASE IF NOT EXISTS eye_research_dataset CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

应用在 `.env` / `.secrets/local.env` 中的连接串可参考（密码为空时使用 `root:`）：

```text
DATABASE_URL=mysql+pymysql://root:@127.0.0.1:3306/eye_research_dataset?charset=utf8mb4
```

**安全说明**：上述空密码、`root` 等配置仅适用于**本机开发**；请勿在测试/生产沿用。远程环境请使用 TiDB/MySQL 独立账号与强密码，并写入受控密钥渠道（勿提交仓库）。

详细说明归档：`docs/development/本地开发环境数据库_MySQL.md`。

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

## 本机磁盘与 `.runtime` 清理

联调与集成测试会在仓库根目录 `.runtime/`（默认 `DATASET_RUNTIME_DIR`）积累上传分片、合并 zip、解压与解析产物等，体量可达数十 GB；**与** `test-data/` 下的大样例包相互独立。腾空间前应先停止本机 API / 本地 FTP（常见端口 8092、2121），再删除 `.runtime`。详细命令与工作区与测试服务器的区分见工作区内运维手册 **`docs/ops/部署与运维手册_正式版_20260513.md` 附录 A**（版本 V1.1.2+）；清理操作记录见 **`docs/work-logs/2026-05-12_数据集管理新视野正式版三轮验收修复与部署文档整理.md`** §11.7。
