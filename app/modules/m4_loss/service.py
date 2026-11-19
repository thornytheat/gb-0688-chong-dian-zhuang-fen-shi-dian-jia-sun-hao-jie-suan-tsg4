from .rules import allocate
from decimal import Decimal as D
def execute(ctx):
    orders,balances=allocate(ctx['records']);ctx['orders']=orders;ctx['loss_balances']=balances
    ctx['stages'].append({'title':'站内损耗','rows':balances,'metrics':{'站点':len(balances),'损耗 kWh':str(sum((r['loss_kwh'] for r in balances),0)),'充电 kWh':str(sum((r.get('charge_kwh',0) for r in balances),0)),'放电 kWh':str(sum((r.get('discharge_kwh',0) for r in balances),0)),'异常站':sum(r['state']!='通过' for r in balances)}});return ctx
