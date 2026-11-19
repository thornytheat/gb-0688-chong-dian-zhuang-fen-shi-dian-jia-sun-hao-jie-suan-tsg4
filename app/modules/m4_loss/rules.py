from decimal import Decimal as D
from collections import defaultdict
from app.modules.m8_daily.rules import settle
def allocate(rows):
    # 按 +08 本地日做分向损耗（与 M8 同口径），再汇总回站点，保证整站与按日结果相互追溯。
    days,flows=settle(rows)
    flow_map=defaultdict(list)
    for f in flows:flow_map[(f['station'],f['day'])].append(f)
    stations=defaultdict(list)
    for row in rows:stations[row['station']].append(row)
    assigned=[];balances=[]
    for station,group in sorted(stations.items()):
        orders=[r for r in group if r['kind']=='order'];meters=[r for r in group if r['kind']=='station']
        meter=sum((r['energy'] for r in meters),D(0))
        charge_orders=sorted([r for r in orders if r.get('direction','charge')=='charge'],key=lambda r:r['id'])
        discharge_orders=sorted([r for r in orders if r.get('direction')=='discharge'],key=lambda r:r['id'])
        charge_kwh=sum((r['energy'] for r in charge_orders),D(0))
        discharge_kwh=sum((r['energy'] for r in discharge_orders),D(0))
        station_days=[d for d in days if d['station']==station]
        state='通过'
        bad=[d for d in station_days if d['state']!='通过']
        if bad:state=bad[0]['state']
        # 逐单损耗：把该单在各本地日的份额相加（跨日单自动累加）；被拦截日份额为 0。
        per_order=defaultdict(lambda:D(0))
        blocked = state!='通过'
        for d in station_days:
            for f in flow_map[(station,d['day'])]:
                per_order[f['order'] or f['id']]+=D(0) if blocked else f['loss_kwh']
        for row in charge_orders:assigned.append({**row,'loss_kwh':per_order.get(row.get('order') or row['id'],D(0)),'loss_state':state})
        for row in discharge_orders:assigned.append({**row,'loss_kwh':per_order.get(row.get('order') or row['id'],D(0)),'loss_state':state})
        loss=meter-(charge_kwh-discharge_kwh)
        if blocked or charge_kwh+discharge_kwh==0:
            charge_loss=D(0);discharge_loss=D(0)
        else:
            # 与 M8 同口径的分向切分，但在整站总量上计算一次，避免跨日除法尾差。
            charge_loss=loss*charge_kwh/(charge_kwh+discharge_kwh)
            discharge_loss=loss-charge_loss
        balances.append({'station':station,'meter_kwh':meter,'order_kwh':charge_kwh-discharge_kwh,
                         'charge_kwh':charge_kwh,'discharge_kwh':discharge_kwh,
                         'charge_loss_kwh':charge_loss,'discharge_loss_kwh':discharge_loss,
                         'loss_kwh':loss,'state':state})
    return assigned,balances
