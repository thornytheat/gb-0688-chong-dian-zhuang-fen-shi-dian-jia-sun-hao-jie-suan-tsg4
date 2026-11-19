import sqlite3
from decimal import Decimal as D

import pytest

from app.config import SAMPLES
from app.errors import DomainError
from app.normalize import instant
from app.pipeline import run
from app import store
from app.main import create_app
from app.modules.m8_daily.rules import settle, slice_by_local_day
from app.modules.m8_daily import service


def rec(id, kind, energy, start='2026-11-16T00:00:00+08:00', end='2026-11-17T00:00:00+08:00',
        station='S', direction='charge', order=None):
    return dict(id=id, station=station, kind=kind, energy=D(energy),
                start=instant(start), end=instant(end), direction=direction,
                order=order or ('O' + id), unit='kWh')


# ---------- 规则层：分向损耗与守恒 ----------

def test_discharge_does_not_bear_charge_loss():
    # 充电 40、放电 10、总表净量 36 → 损耗 6，按各方向电量占比切分 4.8 / 1.2
    rows = [rec('C1', 'order', '30'), rec('C2', 'order', '10'),
            rec('D1', 'order', '10', direction='discharge'),
            rec('M', 'station', '36', order='')]
    days, flows = settle(rows)
    day = days[0]
    assert day['state'] == '通过'
    assert day['loss_kwh'] == D('6')
    assert day['charge_loss_kwh'] == D('4.8')
    assert day['discharge_loss_kwh'] == D('1.2')
    # 总表净量 = 充电 − 放电 + 损耗，可逐级追溯
    assert day['net_kwh'] == D('36') == day['charge_kwh'] - day['discharge_kwh'] + day['loss_kwh']
    by_order = {f['order']: f for f in flows}
    assert by_order['OC1']['loss_kwh'] == D('3.6')
    assert by_order['OC2']['loss_kwh'] == D('1.2')
    assert by_order['OD1']['loss_kwh'] == D('1.2')  # 放电单只承担放电侧 1.2，不承担整站 6
    assert by_order['OD1']['net_kwh'] == D('-8.8')


def test_charge_only_keeps_legacy_behaviour():
    days, flows = settle([rec('C1', 'order', '10'), rec('M', 'station', '11', order='')])
    assert days[0]['charge_loss_kwh'] == D('1')
    assert days[0]['discharge_loss_kwh'] == D(0)
    assert flows[0]['loss_kwh'] == D('1')


def test_local_day_slice_and_timezone_equivalence():
    pieces = slice_by_local_day(rec('M', 'station', '10',
                                    start='2026-11-16T12:00:00+08:00',
                                    end='2026-11-17T12:00:00+08:00', order=''))
    assert [p['day'] for p in pieces] == ['2026-11-16', '2026-11-17']
    assert [p['kwh'] for p in pieces] == [D('5'), D('5')]
    utc = slice_by_local_day(rec('M2', 'station', '10',
                                 start='2026-11-16T04:00:00+00:00',
                                 end='2026-11-17T04:00:00+00:00', order=''))
    assert utc == pieces


def test_blocked_states_skip_allocation():
    days, flows = settle([rec('C1', 'order', '10'), rec('M', 'station', '9', order='')])
    assert days[0]['state'] == '订单电量超过总表'
    assert all(f['loss_kwh'] == 0 for f in flows)
    assert days[0]['net_kwh'] == days[0]['charge_kwh'] - days[0]['discharge_kwh'] + days[0]['loss_kwh']
    assert settle([rec('M', 'station', '10', order='')])[0][0]['state'] == '无订单承接损耗'


def test_conservation_guard_rejects_broken_allocation(monkeypatch):
    import app.modules.m8_daily.rules as rules
    monkeypatch.setattr(rules, '_allocate_direction',
                        lambda orders, loss: [D('0.001') for _ in orders] if orders else [])
    with pytest.raises(DomainError) as exc:
        settle([rec('C1', 'order', '10'), rec('M', 'station', '11', order='')])
    assert exc.value.code == 'INCOMPLETE'


def test_direction_defaults_to_charge_without_column(raw):
    # 旧样例没有 direction 列，全部按充电处理且能跑通 M8
    result = run(raw, stop=8, persist=False)
    assert len(result['stages']) == 8
    assert result['daily_flows']
    assert all(f['direction'] == 'charge' for f in result['daily_flows'])
    for day in result['daily']:
        assert D(day['net_kwh']) == D(day['charge_kwh']) - D(day['discharge_kwh']) + D(day['loss_kwh'])


# ---------- 持久化：唯一、幂等、重算、归档、回滚、锁定 ----------

@pytest.fixture
def bidir_text():
    return (SAMPLES / 'bidirectional.csv').read_text()


def _actions(reply):
    return {(a['station'], a['day']): a['action'] for a in reply['actions']}


def test_settle_new_then_idempotent(isolate, bidir_text):
    saved, actions, failures, _ = service.persist(run(bidir_text, stop=8, persist=False)['records'], 'v')
    assert not failures
    assert all(a['action'] == '新建' for a in actions)
    saved2, actions2, failures2, _ = service.persist(run(bidir_text, stop=8, persist=False)['records'], 'v')
    assert not failures2
    assert all(a['action'] == '幂等' for a in actions2)
    # 重试同一天不新增结果：同站同日仍只有一条，版本仍为 1
    rows = store.list_daily()
    assert len(rows) == len(saved) == len(saved2)
    assert all(r['version'] == 1 for r in rows)


def test_station_day_is_unique_in_db(isolate, bidir_text):
    service.persist(run(bidir_text, stop=8, persist=False)['records'], 'v')
    with pytest.raises(sqlite3.IntegrityError):
        with store.connection() as db:
            db.execute("INSERT INTO daily_settlements(station,day,result_id,input_hash,rules_version,payload,updated_at)"
                       " VALUES(?,?,?,?,?,?,?)", ('站甲', '2026-11-16', 'x', 'h', 'v', '{}', 'now'))


def test_late_discharge_recomputes_only_affected_day(isolate, bidir_text):
    records = run(bidir_text, stop=8, persist=False)['records']
    service.persist(records, 'v')
    late_text = bidir_text + ('D0099,站乙,OD0099,2026-11-16T18:00:00+08:00,2026-11-16T19:00:00+08:00,'
                              '2,kWh,1.8,1,order,discharge\n')
    _, actions, failures, _ = service.persist(run(late_text, stop=8, persist=False)['records'], 'v')
    assert not failures
    mapping = {(a['station'], a['day']): a['action'] for a in actions}
    assert mapping[('站乙', '2026-11-16')] == '重算'
    assert mapping[('站甲', '2026-11-16')] == '幂等'
    assert mapping[('站甲', '2026-11-17')] == '幂等'
    current = store.fetch_daily('站乙', '2026-11-16')
    assert current['version'] == 2 and current['discharge_kwh'] == '10'
    assert any(f['order'] == 'OD0099' for f in current['flows'])
    # 旧版日结原样可查
    archived = store.fetch_version('站乙', '2026-11-16', 1)
    assert archived['discharge_kwh'] == '8'
    assert all(f['order'] != 'OD0099' for f in archived['flows'])
    assert [v['version'] for v in store.list_versions('站乙', '2026-11-16')] == [1]


def test_recompute_failure_restores_previous_daily(isolate, bidir_text, monkeypatch):
    records = run(bidir_text, stop=8, persist=False)['records']
    service.persist(records, 'v')
    before = store.fetch_daily('站乙', '2026-11-16')
    real = store.upsert_daily

    def boom(day_result, flows, input_hash, rules_version):
        if day_result['station'] == '站乙' and day_result['day'] == '2026-11-16':
            raise RuntimeError('disk full simulated')
        return real(day_result, flows, input_hash, rules_version)

    monkeypatch.setattr(store, 'upsert_daily', boom)
    monkeypatch.setattr(service.store, 'upsert_daily', boom)
    late_text = bidir_text + ('D0099,站乙,OD0099,2026-11-16T18:00:00+08:00,2026-11-16T19:00:00+08:00,'
                              '2,kWh,1.8,1,order,discharge\n')
    _, actions, failures, _ = service.persist(run(late_text, stop=8, persist=False)['records'], 'v')
    assert failures and failures[0]['station'] == '站乙'
    after = store.fetch_daily('站乙', '2026-11-16')
    # 旧版日结原样保留，没有归档半条，没有混入晚到单
    assert after['version'] == before['version'] == 1
    assert after['discharge_kwh'] == before['discharge_kwh'] == '8'
    assert store.list_versions('站乙', '2026-11-16') == []
    assert any(e['event'] == '重算失败' for e in store.list_events('站乙', '2026-11-16'))


def test_reviewed_daily_is_locked_and_stale_review_rejected(isolate, bidir_text):
    service.persist(run(bidir_text, stop=8, persist=False)['records'], 'v')
    reviewed = store.review_daily('站甲', '2026-11-16', 1, '通过')
    assert reviewed['review'] == '通过' and reviewed['version'] == 2
    with pytest.raises(DomainError) as exc:
        store.review_daily('站甲', '2026-11-16', 1, '通过')
    assert exc.value.status == 409
    late_text = bidir_text + ('D0100,站甲,OD0100,2026-11-16T20:00:00+08:00,2026-11-16T21:00:00+08:00,'
                              '3,kWh,2.7,1,order,discharge\n')
    _, actions, failures, _ = service.persist(run(late_text, stop=8, persist=False)['records'], 'v')
    assert not failures
    locked = next(a for a in actions if a['station'] == '站甲' and a['day'] == '2026-11-16')
    assert locked['action'] == '锁定' and locked['version'] == 2
    untouched = store.fetch_daily('站甲', '2026-11-16')
    assert untouched['review'] == '通过'
    assert all(f['order'] != 'OD0100' for f in untouched['flows'])


# ---------- HTTP 层 ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    return create_app().test_client()


def test_http_preview_settle_and_rollback_status(client, bidir_text):
    preview = client.post('/api/m8', json={'text': bidir_text})
    assert preview.status_code == 200 and len(preview.json['stages']) == 8
    ok = client.post('/api/m8/settle', json={'text': bidir_text})
    assert ok.status_code == 200 and not ok.json['failures']
    again = client.post('/api/m8/settle', json={'text': bidir_text})
    assert all(a['action'] == '幂等' for a in again.json['actions'])
    listing = client.get('/api/m8/daily')
    assert listing.status_code == 200 and len(listing.json) == 3
    detail = client.get('/api/m8/daily/站甲/2026-11-16')
    assert detail.status_code == 200 and detail.json['flows']
    assert client.get('/api/m8/events').status_code == 200


def test_http_rollback_returns_409(client, bidir_text, monkeypatch):
    client.post('/api/m8/settle', json={'text': bidir_text})
    real = store.upsert_daily
    def boom(day_result, flows, input_hash, rules_version):
        if day_result['station'] == '站乙':
            raise RuntimeError('boom')
        return real(day_result, flows, input_hash, rules_version)
    monkeypatch.setattr(store, 'upsert_daily', boom)
    monkeypatch.setattr(service.store, 'upsert_daily', boom)
    late = bidir_text + ('D0099,站乙,OD0099,2026-11-16T18:00:00+08:00,2026-11-16T19:00:00+08:00,'
                         '2,kWh,1.8,1,order,discharge\n')
    reply = client.post('/api/m8/settle', json={'text': late})
    assert reply.status_code == 409 and reply.json['code'] == 'DAILY_ROLLBACK'
    assert reply.json['failures']


def test_http_review_and_pages(client, bidir_text):
    client.post('/api/m8/settle', json={'text': bidir_text})
    r = client.post('/api/m8/review/站甲/2026-11-16', json={'version': 1, 'decision': '通过'})
    assert r.status_code == 200 and r.json['review'] == '通过'
    stale = client.post('/api/m8/review/站甲/2026-11-16', json={'version': 1, 'decision': '通过'})
    assert stale.status_code == 409
    page = client.get('/m/8')
    assert page.status_code == 200 and '按订单看充放电去向'.encode() in page.data
    for stage in range(1, 9):
        assert client.get(f'/m/{stage}').status_code == 200


# ---------- 样例端到端 ----------

def test_bidirectional_sample_conserves():
    raw = (SAMPLES / 'bidirectional.csv').read_bytes()
    result = run(raw, stop=8, persist=False)
    assert not result['quarantine']
    assert len(result['daily']) == 3
    for day in result['daily']:
        assert day['state'] == '通过'
        assert D(day['net_kwh']) == D(day['charge_kwh']) - D(day['discharge_kwh']) + D(day['loss_kwh'])
        assert D(day['loss_kwh']) == D(day['charge_loss_kwh']) + D(day['discharge_loss_kwh'])
    flows = result['daily_flows']
    assert {f['direction'] for f in flows} == {'charge', 'discharge'}
    assert all(f['net_kwh'].startswith('-') for f in flows if f['direction'] == 'discharge')
