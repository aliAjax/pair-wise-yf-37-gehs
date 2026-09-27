# 传染病暴发调查与接触网络

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8303`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `src/seed.py`：空库时写入演示数据（可用`--no-seed`关闭）。
- `static/index.html`：演示页面（含暴露后预防发放操作区）。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8303
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。数据库为空时自动写入演示数据（含有效/超窗接触者和两个批号），加`--no-seed`可关闭。

## 核心对象

- `case`：病例和调查状态；`contact`：接触者随访。
- `drug_lot`：药品批号与库存（`quantity_remaining`），仅管理员可登记。
- `pep_dispense`：暴露后预防发放单，只能通过专用接口产生。

## 暴露后预防（PEP）发放

`POST /api/pep_dispense`，请求体`{"contact_id","lot_id","course_days"}`，仅`clinician`/`admin`角色可调用。满足以下全部条件才发药：

- 关联病例已确诊（`confirmed`）或临床诊断（`probable`）；
- 接触者仍在观察中（`identified`/`following`，未完成随访）；
- 从`exposure_start`起未满72小时；
- 批号有效且库存不少于疗程天数（1天=1份）。

扣库存与建单在同一个事务内完成：库存不足返回`409 StockShortage`，不扣药也不产生发放单。同一接触者仅允许一份`active`发放单（数据库部分唯一索引保证），重复提交返回`200`与原单据，不重复发药。发放与扣减均写入审计。批号库存可通过`GET /api/drug_lots`查看，发放单通过`GET /api/pep_dispenses`查询。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/pep_dispense`：暴露后预防发放（见上节）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
