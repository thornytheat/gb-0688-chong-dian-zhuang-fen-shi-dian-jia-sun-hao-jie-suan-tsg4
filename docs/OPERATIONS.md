# 运行维护

主服务端口与Compose保持一致。数据保存在records命名卷，应用代码不需要写入权限。

```sh
docker compose logs --tail=80 app
docker compose restart app
docker compose exec -T app pytest -q
docker compose down --remove-orphans
```

健康检查GET /health。失败输入返回原因码，结构性错误422，过大文件413，陈旧复核409。
文件内容最大4MB。批次先计算再原子保存，失败不留半条记录。证据CSV按原始行号定位。

## 充放日结（M8）

日结表 `daily_settlements` 以“站点×日期”为主键，版本归档表 `daily_settlement_versions` 保存每次重算前的旧版，`daily_events` 留存全部动作。重算在单事务内完成，失败回滚并写“重算失败”事件，接口返回409 `DAILY_ROLLBACK`，旧版日结原样可查。

- 预览（不落库）：`GET/POST /api/m8`
- 入库（同站同日唯一）：`POST /api/m8/settle`，动作取 新建/幂等/重算/锁定
- 已入库清单：`GET /api/m8/daily`；单份详情含版本与事件：`GET /api/m8/daily/<station>/<day>`
- 取回历史版本：`GET /api/m8/daily/<station>/<day>/versions/<version>`
- 事件流：`GET /api/m8/events?station=&day=`
- 日结复核（乐观锁，已复核即锁定）：`POST /api/m8/review/<station>/<day>`，陈旧版本返回409

已复核日结不会被晚到数据覆盖，需先退回或按新版本另行处理；回滚事件中的站点日期即恢复旧版的范围。
