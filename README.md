# 航班中断恢复系统

独立的 Python 标准库项目，用 SQLite 保存机场、飞机、机组、航线许可、航班、中断事件和恢复方案。系统会校验维护间隔、执勤时限、机场宵禁、航线许可、资源重叠，并计算取消、延误、受影响旅客和错失衔接成本。

## 运行

```bash
python3 app.py --db airline_recovery.db
```

默认监听 `127.0.0.1:8202`，首页为 `/`，机组接替补班页面为 `/crew`，健康检查为 `/health`。

身份头：`X-User-Id`、`X-Role`。角色包括 `viewer`、`scheduler`、`ops_manager`、`auditor`。

## 主要接口

- `POST /api/airports`、`/api/aircraft`、`/api/crew`、`/api/permits`：基础资源与约束。
- `POST /api/flights`、`POST /api/disruptions`：创建航班和中断。
- `POST /api/recovery-plans`：一次提交方案及航班调整。
- `POST /api/plans/{id}/assignments`：用 `expected_revision` 临时改派。
- `POST /api/plans/{id}/validate`、`/lock`：校验并原子锁定方案。
- `GET /api/disruptions/{id}/compare`：比较恢复方案成本。
- `POST /api/flights/{id}/cancel`、`/recover`：取消和人工恢复。
- `GET /api/state`、`GET /api/plans/{id}`：查询状态和影响。

## 机组接替补班

调整后不必等到锁定才发现执勤超限：

- 纯逻辑在 `duty.py`：按机组签到时间逐段累计执勤，标出超限航段（累计分钟、超限分钟、剩余分钟）。
- 接替判定在 `relief.py`：后备机组（`crew.status=reserve`）必须基地与航段起飞机场一致、与方案内/已锁定方案/方案外实际排班时段不冲突，且把该段并入后继续累计不超本人上限。
- 页面入口在 `static/crew.html`（路由 `/crew`）：按方案列出超限机组与航段、每个航段的可接替人选（含淘汰原因）和换班记录，支持单段接替与一键接替。
- `GET /api/plans/{id}/crew-duty`：累计执勤、超限航段与候选后备；`POST /api/plans/{id}/relieve`（body：`expected_revision`、可选 `assignment_id`、`crew_id`，一键接替补 `__auto__`）执行接替，成功后原机组当场释放该段、后备继续累计，仍超限则整笔事务回滚拦截。
- 后备机组通过 `/api/crew` 以 `status: "reserve"` 录入。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

时区和机场本地时刻没有引入完整时区数据库；模型使用简化航线许可与宵禁规则。身份头、SQLite 和单进程 HTTP 服务适合原型演示，正式运行需要外部身份系统、共享数据库和更强的跨实例锁。
