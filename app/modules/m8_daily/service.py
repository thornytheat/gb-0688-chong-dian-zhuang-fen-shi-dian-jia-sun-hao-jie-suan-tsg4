import hashlib
from app import store
from .rules import settle, slice_by_local_day


def _fingerprint(parts):
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()


def input_hash(records):
    """全局输入摘要：用于接口回执与页面展示。"""
    keys = ('id', 'station', 'order', 'start', 'end', 'energy', 'unit', 'bill', 'version', 'kind', 'direction')
    return _fingerprint(['|'.join(str(r.get(k, '')) for k in keys) for r in records])


def day_input_hashes(records):
    """每个“站点 × 本地日”的独立摘要：只有真正影响该日结的计量变化才触发重算。"""
    hashes = {}
    for row in records:
        for piece in slice_by_local_day(row):
            key = (row['station'], piece['day'])
            line = '|'.join(str(row.get(k, '')) for k in
                            ('id', 'order', 'start', 'end', 'unit', 'bill', 'version', 'kind', 'direction'))
            hashes.setdefault(key, []).append(f"{line}|{piece['kwh']}")
    return {key: _fingerprint(parts) for key, parts in hashes.items()}


def build(records):
    """纯计算：返回 (日结行, 逐单去向)；守恒失败抛 INCOMPLETE，不落任何数据。"""
    return settle(records)


def persist(records, rules_version):
    """逐日原子写入。某日重算失败时记录事件并恢复旧版日结，其它日不受影响。"""
    days, flows = build(records)
    digest = input_hash(records)
    day_hashes = day_input_hashes(records)
    flow_index = {}
    for flow in flows:
        flow_index.setdefault((flow['station'], flow['day']), []).append(flow)
    saved, actions, failures = [], [], []
    for day in days:
        key = (day['station'], day['day'])
        try:
            row, action = store.upsert_daily(day, flow_index.get(key, []), day_hashes[key], rules_version)
            saved.append(row)
            actions.append({'station': key[0], 'day': key[1], 'action': action,
                            'result_id': row['result_id'], 'version': row['version'], 'review': row['review']})
        except Exception as exc:  # 存储层已回滚，旧版日结原样可查
            store.record_daily_event(key[0], key[1], '重算失败', '', f'已恢复旧版日结：{exc}')
            failures.append({'station': key[0], 'day': key[1], 'message': str(exc)})
    return saved, actions, failures, digest


def execute(ctx):
    """流水线预览：只计算不落库，入库由 /api/m8/settle 显式触发。"""
    days, flows = build(ctx['records'])
    ctx['daily'] = days
    ctx['daily_flows'] = flows
    ctx['daily_input_hash'] = input_hash(ctx['records'])
    blocked = sum(1 for r in days if r['state'] != '通过')
    ctx['stages'].append({
        'title': '同站按日充放电日结', 'rows': days,
        'metrics': {'日结': len(days), '待复核日结': blocked, '订单去向': len(flows)},
    })
    return ctx
