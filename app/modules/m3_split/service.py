from .rules import split
from app.errors import DomainError
from app.reasons import rejected
from decimal import Decimal as D
def execute(ctx):
    parts=[];good=[]
    for row in ctx['records']:
        if row['kind']=='station':good.append(row);continue
        try:parts.extend(split(row,ctx['tariffs']));good.append(row)
        except DomainError as exc:ctx['quarantine'].append(rejected(row,exc.code,exc.message))
    ctx['records']=good;ctx['segments']=parts
    charge=sum((r['energy'] for r in good if r['kind']=='order' and r.get('direction')=='charge'),D(0))
    discharge=sum((r['energy'] for r in good if r['kind']=='order' and r.get('direction')=='discharge'),D(0))
    ctx['stages'].append({'title':'订单分时','rows':parts,'metrics':{'分段':len(parts),'订单':sum(r['kind']=='order' for r in good),'充电 kWh':str(charge),'放电 kWh':str(discharge)}});return ctx
