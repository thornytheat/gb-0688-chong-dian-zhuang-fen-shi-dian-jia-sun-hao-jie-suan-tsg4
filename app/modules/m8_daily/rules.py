"""同站按日充放电方向结算规则。

总表只计量净电量（充入与放出相抵后的净值），订单分充电单与放电单。
本模块按 +08:00 本地日把计量与订单切到“站点 × 日期”，保证：

    总表净量 = 充电订单量 − 放电订单量 + 损耗
    损耗     = 充电侧损耗 + 放电侧损耗
    订单损耗份额之和 = 同方向站级损耗

放电单只承担放电侧损耗，不再替充电单承担损耗；每个站点每天只有一份
可结算结果，所有数字使用 Decimal 精确计算并可逐级追溯。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from collections import defaultdict

from app.errors import DomainError

ZONE = timezone(timedelta(hours=8))
DIRECTIONS = ('charge', 'discharge')
DIRECTION_LABEL = {'charge': '充电', 'discharge': '放电'}


def _instant(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _local_day_bounds(day):
    start = datetime.combine(day, datetime.min.time(), ZONE)
    return start, start + timedelta(days=1)


def slice_by_local_day(row):
    """按 +08:00 本地日切分一条计量，电量按段内时长比例分配，尾差并入末段。"""
    start, end = _instant(row['start']), _instant(row['end'])
    energy = row['energy'] if isinstance(row['energy'], D) else D(str(row['energy']))
    cursor = start
    total_seconds = D(str((end - cursor).total_seconds()))
    pieces = []
    while cursor < end:
        local = cursor.astimezone(ZONE)
        day_start, day_end = _local_day_bounds(local.date())
        stop = min(end, day_end)
        fraction = D(str((stop - cursor).total_seconds())) / total_seconds
        pieces.append({'day': local.date().isoformat(), 'kwh': energy * fraction,
                       'day_start': day_start, 'day_end': day_end, 'seg_start': cursor, 'seg_end': stop})
        cursor = stop
    if pieces:
        pieces[-1]['kwh'] += energy - sum((p['kwh'] for p in pieces), D(0))
    return pieces


def _allocate_direction(orders, direction_loss):
    """同方向内按订单电量占比分摊该方向的站级损耗，尾差并入末单。"""
    consumed = sum((r['kwh'] for r in orders), D(0))
    shares = [D(0) for _ in orders]
    if not orders or direction_loss == 0:
        return shares
    shares = [direction_loss * r['kwh'] / consumed for r in orders]
    shares[-1] += direction_loss - sum(shares, D(0))
    return shares


def settle(records):
    """生成同站按日的唯一可结算结果与逐单充放电去向。"""
    grouped = defaultdict(lambda: {'meters': [], 'orders': []})
    for row in records:
        for piece in slice_by_local_day(row):
            key = (row['station'], piece['day'])
            bucket = grouped[key]
            entry = {'id': row['id'], 'order': row.get('order', ''), 'kwh': piece['kwh'],
                     'seg_start': piece['seg_start'], 'seg_end': piece['seg_end'],
                     'day_start': piece['day_start'], 'day_end': piece['day_end']}
            if row['kind'] == 'station':
                bucket['meters'].append(entry)
            else:
                entry['direction'] = row.get('direction', 'charge')
                entry['order'] = row.get('order') or row['id']
                bucket['orders'].append(entry)

    days = []
    order_flows = []
    for (station, day), bucket in sorted(grouped.items()):
        meters = sorted(bucket['meters'], key=lambda r: r['id'])
        orders = sorted(bucket['orders'], key=lambda r: (r['order'], r['id']))
        meter_kwh = sum((r['kwh'] for r in meters), D(0))
        by_direction = {d: [r for r in orders if r['direction'] == d] for d in DIRECTIONS}
        charge_kwh = sum((r['kwh'] for r in by_direction['charge']), D(0))
        discharge_kwh = sum((r['kwh'] for r in by_direction['discharge']), D(0))
        net_order_kwh = charge_kwh - discharge_kwh
        loss_kwh = meter_kwh - net_order_kwh

        state = '通过'
        if len(meters) != 1:
            state = '总表缺失或重复'
        elif any(_instant(r['seg_start']) < _instant(meters[0]['seg_start']) or
                 _instant(r['seg_end']) > _instant(meters[0]['seg_end']) for r in orders):
            state = '总表未覆盖订单区间'
        elif loss_kwh < 0:
            state = '订单电量超过总表'
        elif not orders and loss_kwh > 0:
            state = '无订单承接损耗'

        # 分向损耗：按充/放各自电量占比切分站级损耗，放电侧不替充电侧承担。
        direction_total = charge_kwh + discharge_kwh
        direction_loss = {d: D(0) for d in DIRECTIONS}
        if state == '通过' and direction_total > 0:
            for d in DIRECTIONS:
                side = charge_kwh if d == 'charge' else discharge_kwh
                direction_loss[d] = loss_kwh * side / direction_total
            direction_loss['discharge'] = loss_kwh - direction_loss['charge']

        flows = []
        for d in DIRECTIONS:
            shares = _allocate_direction(by_direction[d], direction_loss[d])
            for row, share in zip(by_direction[d], shares):
                flow = {'station': station, 'day': day, 'order': row['order'], 'id': row['id'],
                        'direction': d, 'direction_label': DIRECTION_LABEL[d],
                        'energy_kwh': row['kwh'], 'loss_kwh': share,
                        'net_kwh': row['kwh'] - share if d == 'charge' else -(row['kwh'] - share),
                        'state': state}
                flows.append(flow)
        order_flows.extend(flows)

        day_result = {
            'station': station, 'day': day, 'state': state,
            'meter_kwh': meter_kwh,
            'charge_kwh': charge_kwh,
            'discharge_kwh': discharge_kwh,
            'net_order_kwh': net_order_kwh,
            'charge_loss_kwh': direction_loss['charge'],
            'discharge_loss_kwh': direction_loss['discharge'],
            'loss_kwh': loss_kwh,
            'net_kwh': meter_kwh,
            'meter_ids': sorted({r['id'] for r in meters}),
            'order_count': len(orders),
            'flow_ids': sorted({r['id'] for r in flows}),
        }
        _assert_conservation(day_result, flows)
        days.append(day_result)
    return days, order_flows


def _assert_conservation(day, flows):
    """守恒断言：总表净量、分向电量、损耗归属必须相互对上，否则整批不结算。"""
    # 代数恒等在任何状态下都成立：总表净量 = 充电 − 放电 + 损耗。
    identity = day['charge_kwh'] - day['discharge_kwh'] + day['loss_kwh']
    if day['net_kwh'] != identity:
        raise DomainError('INCOMPLETE', f"{day['station']} {day['day']} 总表净量无法追溯到订单与损耗")
    if day['state'] != '通过':
        return
    charge_loss = sum((r['loss_kwh'] for r in flows if r['direction'] == 'charge'), D(0))
    discharge_loss = sum((r['loss_kwh'] for r in flows if r['direction'] == 'discharge'), D(0))
    checks = [
        (day['charge_loss_kwh'], charge_loss, '充电侧损耗分摊不守恒'),
        (day['discharge_loss_kwh'], discharge_loss, '放电侧损耗分摊不守恒'),
        (day['loss_kwh'], charge_loss + discharge_loss, '站级损耗与分向损耗不符'),
    ]
    for expected, actual, message in checks:
        if expected != actual:
            raise DomainError('INCOMPLETE', f"{day['station']} {day['day']} {message}")
