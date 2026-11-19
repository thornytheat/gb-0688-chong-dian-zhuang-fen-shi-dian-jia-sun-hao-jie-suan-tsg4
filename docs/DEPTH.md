# 深度登记 充电桩分时电价与损耗结算

- 规模：中型
- 核心规则：电量按计量段与费率区间交集时长分配，跨日费率按当地日期选择，未知电量不作零，损耗分到分
- 使用者与工作流：充电站财务核算员；接收计量间隔与订单，按实际时段重算电费及站内损耗

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 电量接收 | 接收订单及总表计量 | `/api/m1` | `app/modules/m1_intake/rules.py` `app/modules/m1_intake/service.py` | `tests/test_m1_intake.py` |
| M2 费率版本 | 校核费率版本与单位 | `/api/m2` | `app/modules/m2_tariffs/rules.py` `app/modules/m2_tariffs/service.py` | `tests/test_m2_tariffs.py` |
| M3 订单切段 | 按时段交集拆分电量 | `/api/m3` | `app/modules/m3_split/rules.py` `app/modules/m3_split/service.py` | `tests/test_m3_split.py` |
| M4 站内损耗 | 按站点分配总表损耗 | `/api/m4` | `app/modules/m4_loss/rules.py` `app/modules/m4_loss/service.py` | `tests/test_m4_loss.py` |
| M5 费用重算 | 精确计算并分配分币 | `/api/m5` | `app/modules/m5_fees/rules.py` `app/modules/m5_fees/service.py` | `tests/test_m5_fees.py` |
| M6 账单对照 | 对照账单与损耗异常 | `/api/m6` | `app/modules/m6_bills/rules.py` `app/modules/m6_bills/service.py` | `tests/test_m6_bills.py` |
| M7 结算批次 | 核对电量行数并复核 | `/api/m7` | `app/modules/m7_batches/rules.py` `app/modules/m7_batches/service.py` | `tests/test_m7_batches.py` |
| M8 充放日结 | 同站按日唯一可结算、分向损耗与版本治理 | `/api/m8` `/api/m8/settle` | `app/modules/m8_daily/rules.py` `app/modules/m8_daily/service.py` `app/store.py` | `tests/test_m8_daily.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-06 | 全角数字 | 数值或归属错误 | 显式契约和守恒检查 | `app/modules/m2_tariffs/rules.py` | `tests/test_m2_tariffs.py::test_fullwidth_energy` |
| TRAP-07 | 未知电量 | 数值或归属错误 | 显式契约和守恒检查 | `app/modules/m2_tariffs/rules.py` | `tests/test_m2_tariffs.py::test_sentinel` |
| TRAP-10 | 浮点累加 | 数值或归属错误 | 显式契约和守恒检查 | `app/modules/m5_fees/rules.py` | `tests/test_m5_fees.py::test_decimal_exact` |
| TRAP-11 | 时区混写 | 数值或归属错误 | 显式契约和守恒检查 | `app/modules/m3_split/rules.py` | `tests/test_m3_split.py::test_timezone_equivalent` |
| TRAP-14 | 同版计量矛盾 | 数值或归属错误 | 显式契约和守恒检查 | `app/modules/m2_tariffs/rules.py` | `tests/test_m2_tariffs.py::test_real_conflict` |
| TRAP-15 | MWh换算 | 数值或归属错误 | 显式契约和守恒检查 | `app/modules/m2_tariffs/rules.py` | `tests/test_m2_tariffs.py::test_mixed_units` |
| TRAP-23 | 分时端点 | 数值或归属错误 | 显式契约和守恒检查 | `app/modules/m3_split/rules.py` | `tests/test_m3_split.py::test_boundary_split` |
| TRAP-24 | 分币尾差 | 数值或归属错误 | 显式契约和守恒检查 | `app/modules/m5_fees/rules.py` | `tests/test_m5_fees.py::test_pennies_conserved` |
| TRAP-25 | 费率版本重叠 | 数值或归属错误 | 显式契约和守恒检查 | `app/modules/m2_tariffs/rules.py` | `tests/test_m2_tariffs.py::test_overlap_versions` |
| TRAP-26 | 反向放电净量相抵 | 放电单替充电单承担损耗、分账与总表净额对不上 | 分向损耗 + 同站按日守恒链 | `app/modules/m8_daily/rules.py` `app/modules/m4_loss/rules.py` | `tests/test_m8_daily.py::test_discharge_does_not_bear_charge_loss` |
| TRAP-27 | 晚到放电单覆盖已结日 | 重算失败留半条、重试产生重复结果、已复核被改 | 按日输入摘要幂等、归档+事务回滚、复核锁定 | `app/store.py` `app/modules/m8_daily/service.py` | `tests/test_m8_daily.py` |

## 对账规则

输入逻辑行数 = 采信行数 + 隔离行数；空行单列。每条重复、冲突、超限、缺失均保留原始行号与原因码。计算阶段的输出数量可聚合，原始采信records始终保留，业务平衡另按模块给出的重量、时长、电量或座席数量核对。
相同输入摘要、规则版本、参数产生相同批次ID。运行原子插入SQLite；复核使用整数版本乐观锁。
M8日结以“站点×+08本地日”为唯一键（daily_settlements主键），独立输入摘要决定新建/幂等/重算/锁定。重算在单事务内先归档旧版到daily_settlement_versions再替换当前行，任一步失败回滚；daily_events留存新建、幂等重试、重算、重算失败、复核全部事件。已复核日行review≠待复核，重算只返回锁定不写入。日结恒等链：总表净量=充电−放电+损耗，损耗=充侧+放侧，方向内订单份额=方向损耗。

## 已知不覆盖

计量段内假定匀速用电，站点总表必须唯一且覆盖所导入的全部订单；真实采样差异应预先切成计量段。只计算电费及损耗，不含停车服务费。
