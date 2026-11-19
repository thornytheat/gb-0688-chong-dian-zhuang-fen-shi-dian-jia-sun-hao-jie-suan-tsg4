import pytest
from decimal import Decimal as D
from app.errors import DomainError
from app.pipeline import run
from app.normalize import instant
from app.modules.m4_loss.rules import allocate
def rec(id,kind,energy):return dict(id=id,station='S',kind=kind,energy=D(energy),start=instant('2026-11-16T00:00:00Z'),end=instant('2026-11-17T00:00:00Z'))

def test_loss_proportion():
    x,b=allocate([rec('1','order','10'),rec('2','order','20'),rec('3','station','33')]);assert [r['loss_kwh'] for r in x]==[1,2] and b[0]['loss_kwh']==3

def test_negative_loss_blocked():
    x,b=allocate([rec('1','order','10'),rec('2','station','9')]);assert x[0]['loss_kwh']==0 and b[0]['state']!='通过'

def test_no_meter():
    assert allocate([rec('1','order','10')])[1][0]['state']!='通过'

def test_zero_orders():
    assert allocate([rec('1','station','10')])[1][0]['state']=='无订单承接损耗'

def test_real_three_stations(raw):
    assert len(run(raw,stop=4,persist=False)['loss_balances'])==3

def test_real_loss_conserved(raw):
    # 被拦截站点不强制分摊损耗（份额为 0）；通过站点的订单份额之和等于其站级损耗
    x=run(raw,stop=4,persist=False)
    for balance in x['loss_balances']:
        if balance['state']!='通过':
            assert all(D(r['loss_kwh'])==0 for r in x['orders'] if r['station']==balance['station'])
            continue
        share=sum((D(r['loss_kwh']) for r in x['orders'] if r['station']==balance['station']),D(0))
        assert share==D(balance['loss_kwh'])

def test_clean_sample_daily_blocked_when_negative_daily_loss():
    # 按日口径：整站净量为正，但某一本地日订单超过当日总表份额，该站标记待复核且不分摊
    from app.config import SAMPLES
    result=run((SAMPLES/'clean.csv').read_bytes(),stop=4,persist=False)
    assert result['input_count']==43 and not result['quarantine']
    assert len(result['loss_balances'])==3
    blocked=[b for b in result['loss_balances'] if b['state']!='通过']
    assert {b['station'] for b in blocked}=={'站乙','站丙'}
    daily=run((SAMPLES/'clean.csv').read_bytes(),stop=8,persist=False)['daily']
    assert any(d['state']=='订单电量超过总表' for d in daily)

def test_directional_loss_split():
    # 充电 40、放电 10、总表 36 → 损耗 6，分向 4.8 / 1.2，放电单不承担充电侧损耗
    rows=[dict(id='C1',station='S',kind='order',energy=D('30'),direction='charge',order='C1',
               start=instant('2026-11-16T00:00:00Z'),end=instant('2026-11-17T00:00:00Z')),
          dict(id='C2',station='S',kind='order',energy=D('10'),direction='charge',order='C2',
               start=instant('2026-11-16T00:00:00Z'),end=instant('2026-11-17T00:00:00Z')),
          dict(id='D1',station='S',kind='order',energy=D('10'),direction='discharge',order='D1',
               start=instant('2026-11-16T00:00:00Z'),end=instant('2026-11-17T00:00:00Z')),
          rec('M','station','36')]
    assigned,balances=allocate(rows)
    b=balances[0]
    assert b['charge_loss_kwh']==D('4.8') and b['discharge_loss_kwh']==D('1.2')
    by={r['order']:r for r in assigned}
    assert by['D1']['loss_kwh']==D('1.2')

def test_meter_must_cover_order_interval():
    order=rec('1','order','10');meter=rec('2','station','11')
    meter['end']=instant('2026-11-16T12:00:00Z')
    assigned,balances=allocate([order,meter])
    assert balances[0]['state']=='总表未覆盖订单区间'
    assert assigned[0]['loss_kwh']==0
